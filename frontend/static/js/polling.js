/* Single-owner, visibility-aware, non-overlapping page polling.
   ES5 syntax is deliberate: the deterministic regression harness also runs it
   through Windows Script Host without a browser. */
(function (root) {
  "use strict";
  var owners = {};

  function noop() {}

  function PollOwner(key, task, options) {
    options = options || {};
    this.key = key;
    this.task = task;
    this.intervalMs = Math.max(1000, Number(options.intervalMs || 7000));
    this.document = options.document || root.document;
    this.setIntervalFn = options.setIntervalFn || root.setInterval;
    this.clearIntervalFn = options.clearIntervalFn || root.clearInterval;
    this.AbortControllerCtor = options.AbortControllerCtor || root.AbortController;
    this.timer = null;
    this.inFlight = false;
    this.controller = null;
    this.cleaned = false;
    this.initialized = false;
    var self = this;
    this._visibility = function () { self._onVisibility(); };
    this._unload = function () { self.cleanup(); };
  }

  PollOwner.prototype._hidden = function () {
    return !!(this.document &&
      (this.document.hidden || this.document.visibilityState === "hidden"));
  };

  PollOwner.prototype._arm = function () {
    var self = this;
    if (this.cleaned || this.timer !== null || this._hidden()) return;
    this.timer = this.setIntervalFn(function () { self.runNow(); }, this.intervalMs);
  };

  PollOwner.prototype._disarm = function () {
    if (this.timer !== null) {
      this.clearIntervalFn(this.timer);
      this.timer = null;
    }
  };

  PollOwner.prototype._finish = function () {
    this.inFlight = false;
    this.controller = null;
  };

  PollOwner.prototype.runNow = function () {
    if (this.cleaned || this._hidden() || this.inFlight) return false;
    this.inFlight = true;
    this.controller = this.AbortControllerCtor ?
      new this.AbortControllerCtor() : { signal: undefined, abort: noop };
    var result;
    try {
      result = this.task(this.controller.signal);
    } catch (err) {
      this._finish();
      return false;
    }
    var self = this;
    if (result && typeof result.then === "function") {
      result.then(function () { self._finish(); }, function () { self._finish(); });
    } else {
      this._finish();
    }
    return true;
  };

  PollOwner.prototype._onVisibility = function () {
    if (this._hidden()) {
      this._disarm();
      if (this.controller && typeof this.controller.abort === "function") {
        this.controller.abort();
      }
      return;
    }
    this._arm();
    this.runNow();
  };

  PollOwner.prototype.start = function () {
    if (this.cleaned || this.initialized) return this;
    this.initialized = true;
    if (this.document && this.document.addEventListener) {
      this.document.addEventListener("visibilitychange", this._visibility);
    }
    if (root.addEventListener) {
      root.addEventListener("pagehide", this._unload);
      root.addEventListener("beforeunload", this._unload);
    }
    this._arm();
    this.runNow(); // exactly one initial request
    return this;
  };

  PollOwner.prototype.cleanup = function () {
    if (this.cleaned) return;
    this.cleaned = true;
    this._disarm();
    if (this.controller && typeof this.controller.abort === "function") {
      this.controller.abort();
    }
    if (this.document && this.document.removeEventListener) {
      this.document.removeEventListener("visibilitychange", this._visibility);
    }
    if (root.removeEventListener) {
      root.removeEventListener("pagehide", this._unload);
      root.removeEventListener("beforeunload", this._unload);
    }
    if (owners[this.key] === this) delete owners[this.key];
  };

  root.AICAMPolling = {
    create: function (key, task, options) {
      if (owners[key] && !owners[key].cleaned) return owners[key];
      owners[key] = new PollOwner(key, task, options);
      return owners[key];
    },
    get: function (key) { return owners[key] || null; },
    cleanupAll: function () {
      var keys = [];
      var key;
      for (key in owners) {
        if (Object.prototype.hasOwnProperty.call(owners, key)) keys.push(key);
      }
      for (var i = 0; i < keys.length; i += 1) owners[keys[i]].cleanup();
    },
    PollOwner: PollOwner
  };
}(this));
