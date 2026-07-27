// Deterministic Windows Script Host regression harness for polling.js.
var timers = {};
var nextTimer = 1;
var intervalRegistrations = 0;
function setInterval(fn, ms) {
  var id = nextTimer++;
  timers[id] = fn;
  intervalRegistrations += 1;
  return id;
}
function clearInterval(id) { delete timers[id]; }
function addEventListener() {}
function removeEventListener() {}

var visibilityListeners = [];
var fakeDocument = {
  hidden: false,
  visibilityState: "visible",
  addEventListener: function (name, fn) {
    if (name === "visibilitychange") visibilityListeners.push(fn);
  },
  removeEventListener: function () {}
};
function AbortController() {
  this.signal = { aborted: false };
  this.abort = function () { this.signal.aborted = true; };
}

var fso = new ActiveXObject("Scripting.FileSystemObject");
var pollingPath = WScript.Arguments.Item(0);
var stream = fso.OpenTextFile(pollingPath, 1);
eval(stream.ReadAll());
stream.Close();

var calls = 0;
var pending = [];
function task() {
  calls += 1;
  var success = null;
  var failure = null;
  var thenable = {
    then: function (ok, bad) { success = ok; failure = bad; }
  };
  pending.push({
    resolve: function () { if (success) success(); },
    reject: function () { if (failure) failure(); }
  });
  return thenable;
}
function assertTrue(value, message) {
  if (!value) {
    WScript.Echo("FAIL: " + message);
    WScript.Quit(1);
  }
}
function tickAll() {
  var copy = [];
  var key;
  for (key in timers) copy.push(timers[key]);
  for (var i = 0; i < copy.length; i += 1) copy[i]();
}

var first = AICAMPolling.create("history", task, {
  intervalMs: 7000,
  document: fakeDocument,
  setIntervalFn: setInterval,
  clearIntervalFn: clearInterval,
  AbortControllerCtor: AbortController
}).start();
var second = AICAMPolling.create("history", task, {}).start();
assertTrue(first === second, "initialization twice must reuse one owner");
assertTrue(intervalRegistrations === 1, "initialization twice must register one timer");
assertTrue(calls === 1, "exactly one initial request expected");

tickAll();
assertTrue(calls === 1, "pending request must prevent overlap");
pending[0].resolve();
tickAll();
assertTrue(calls === 2, "next tick after completion must run");
pending[1].resolve();

fakeDocument.hidden = true;
fakeDocument.visibilityState = "hidden";
visibilityListeners[0]();
tickAll();
assertTrue(calls === 2, "hidden document must pause polling");

fakeDocument.hidden = false;
fakeDocument.visibilityState = "visible";
visibilityListeners[0]();
assertTrue(calls === 3, "visibility resume must fetch exactly once");
assertTrue(intervalRegistrations === 2, "resume must arm exactly one replacement timer");
pending[2].resolve();

first.cleanup();
tickAll();
assertTrue(calls === 3, "cleanup must stop all future polling");
WScript.Echo("PASS");
