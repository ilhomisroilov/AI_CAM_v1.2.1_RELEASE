"""Train the Engraved OCR v1.2.1 character classifier from the authoritative dataset.

21 charset classes (0123456789ABCDEFHJNST) + 1 REJECT/UNKNOWN class = 22 outputs.
Grouped canonical split (no leakage); REJECT samples from reject_manifest (grouped
split too). Grayscale, aspect-preserving pad, deterministic, class-weighted loss,
identity-preserving augmentation, best checkpoint by val macro-F1. Writes all artifacts
+ evaluation to models/engraved_ocr_v1.2.1/. CPU-friendly.
"""
from __future__ import annotations
import csv, os, json, hashlib, random, time, sys
from collections import Counter, defaultdict
import numpy as np
from PIL import Image
import torch, torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import torchvision

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # release worktree
LAB = os.environ.get("AI_CAM_OCR_DATASET_ROOT",
                     os.path.join(WT, "external_ocr_dataset"))
SPLITD = os.path.join(LAB, "character_dataset", "splits", "production_current_v1")
OUT = os.path.join(WT, "models", "engraved_ocr_v1.2.1")
os.makedirs(OUT, exist_ok=True)
CHARSET = "0123456789ABCDEFHJNST"
REJECT_IDX = len(CHARSET)           # 21
NCLASS = len(CHARSET) + 1           # 22
IMG = 64
SEED = 1337
random.seed(SEED); np.random.seed(SEED); torch.manual_seed(SEED)

def rd(p): return list(csv.DictReader(open(p, newline='', encoding='utf-8'))) if os.path.exists(p) else []
def resolve(cp):
    p = (cp or "").replace("\\", "/")
    ap = p if os.path.isabs(p) else os.path.join(LAB, p)
    return ap if os.path.isfile(ap) else None

def load_gray_pad(path):
    im = Image.open(path).convert("L")
    w, h = im.size
    s = IMG / max(w, h)
    nw, nh = max(1, int(round(w*s))), max(1, int(round(h*s)))
    im = im.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("L", (IMG, IMG), 0)
    canvas.paste(im, ((IMG-nw)//2, (IMG-nh)//2))
    return np.asarray(canvas, dtype=np.float32) / 255.0

def grouped_split_reject(rows, frac=(0.70, 0.15, 0.15)):
    groups = defaultdict(list)
    for r in rows:
        g = r.get("duplicate_group_id") or r.get("source_line_id") or r.get("crop_path")
        groups[g].append(r)
    keys = sorted(groups, key=lambda k: -len(groups[k]))
    tot = len(rows); tgt = {"train": frac[0], "validation": frac[1], "test": frac[2]}
    cnt = {"train":0,"validation":0,"test":0}; out = {"train":[],"validation":[],"test":[]}
    for k in keys:
        s = max(tgt, key=lambda s: tgt[s] - cnt[s]/max(1,tot))
        out[s] += groups[k]; cnt[s] += len(groups[k])
    return out

class CropDS(Dataset):
    def __init__(self, items, train=False):
        self.items = items; self.train = train
    def __len__(self): return len(self.items)
    def __getitem__(self, i):
        path, y = self.items[i]
        a = load_gray_pad(path)
        if self.train:
            # identity-preserving aug: brightness/contrast, small affine, mild noise
            if random.random() < 0.5:
                a = np.clip(a*random.uniform(0.85,1.15) + random.uniform(-0.06,0.06), 0, 1)
            if random.random() < 0.3:
                a = np.clip(a + np.random.normal(0, 0.02, a.shape).astype(np.float32), 0, 1)
            t = torch.from_numpy(a).unsqueeze(0).unsqueeze(0)
            ang = random.uniform(-3,3)*np.pi/180; tx=random.uniform(-0.06,0.06); ty=random.uniform(-0.06,0.06)
            sc = random.uniform(0.94,1.06)
            theta = torch.tensor([[np.cos(ang)/sc, -np.sin(ang)/sc, tx],
                                  [np.sin(ang)/sc, np.cos(ang)/sc, ty]], dtype=torch.float32).unsqueeze(0)
            grid = torch.nn.functional.affine_grid(theta, t.shape, align_corners=False)
            a = torch.nn.functional.grid_sample(t, grid, align_corners=False, padding_mode="zeros")[0,0].numpy()
        x = torch.from_numpy(a).unsqueeze(0).repeat(3,1,1)   # 3ch for mobilenet
        x = (x - 0.5) / 0.5
        return x, y

def build_items():
    def items_from(rows):
        out = []
        for r in rows:
            p = resolve(r["crop_path"]); lab = (r.get("character_label") or "").upper()
            if p and lab in CHARSET:
                out.append((p, CHARSET.index(lab)))
        return out
    tr = items_from(rd(os.path.join(SPLITD, "train.csv")))
    va = items_from(rd(os.path.join(SPLITD, "validation.csv")))
    te = items_from(rd(os.path.join(SPLITD, "test.csv")))
    # REJECT class from reject_manifest (grouped split)
    rej = rd(os.path.join(LAB, "character_dataset", "manifests", "reject_manifest.csv"))
    rej = [r for r in rej if resolve(r.get("crop_path"))]
    rsp = grouped_split_reject(rej)
    for s, bucket in (("train",tr),("validation",va),("test",te)):
        for r in rsp[s]:
            bucket.append((resolve(r["crop_path"]), REJECT_IDX))
    return tr, va, te

def macro_f1(y_true, y_pred, n=NCLASS):
    f1s = []
    for c in range(n):
        tp = sum(1 for t,p in zip(y_true,y_pred) if t==c and p==c)
        fp = sum(1 for t,p in zip(y_true,y_pred) if t!=c and p==c)
        fn = sum(1 for t,p in zip(y_true,y_pred) if t==c and p!=c)
        if tp+fp+fn == 0: continue
        prec = tp/(tp+fp) if tp+fp else 0; rec = tp/(tp+fn) if tp+fn else 0
        f1s.append(2*prec*rec/(prec+rec) if prec+rec else 0)
    return float(np.mean(f1s)) if f1s else 0.0

def main():
    tr, va, te = build_items()
    print(f"items: train={len(tr)} val={len(va)} test={len(te)} classes={NCLASS}")
    counts = Counter(y for _,y in tr)
    weights = torch.tensor([1.0/max(1,counts.get(c,0)) for c in range(NCLASS)], dtype=torch.float32)
    weights = weights / weights.sum() * NCLASS
    dl_tr = DataLoader(CropDS(tr, True), batch_size=32, shuffle=True, num_workers=0)
    dl_va = DataLoader(CropDS(va, False), batch_size=64, num_workers=0)
    try:
        net = torchvision.models.mobilenet_v3_small(weights=torchvision.models.MobileNet_V3_Small_Weights.DEFAULT)
        print("loaded pretrained MobileNetV3-Small")
    except Exception as exc:
        net = torchvision.models.mobilenet_v3_small(weights=None)
        print(f"pretrained unavailable ({exc}); training from scratch")
    net.classifier[3] = nn.Linear(net.classifier[3].in_features, NCLASS)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    lossf = nn.CrossEntropyLoss(weight=weights)
    best_f1 = -1; best_state = None; patience = 8; bad = 0; hist = []
    for epoch in range(40):
        net.train()
        for x, y in dl_tr:
            opt.zero_grad(); out = net(x); loss = lossf(out, y); loss.backward(); opt.step()
        net.eval(); yt=[]; yp=[]
        with torch.no_grad():
            for x, y in dl_va:
                p = net(x).argmax(1); yt += y.tolist(); yp += p.tolist()
        f1 = macro_f1(yt, yp); acc = np.mean([a==b for a,b in zip(yt,yp)]) if yt else 0
        hist.append({"epoch":epoch,"val_macro_f1":round(f1,4),"val_acc":round(float(acc),4)})
        print(f"epoch {epoch}: val_macro_f1={f1:.4f} val_acc={acc:.4f}")
        if f1 > best_f1:
            best_f1 = f1; best_state = {k:v.clone() for k,v in net.state_dict().items()}; bad=0
        else:
            bad += 1
            if bad >= patience: print("early stop"); break
    net.load_state_dict(best_state)
    torch.save({"state_dict": net.state_dict(), "charset": CHARSET, "nclass": NCLASS,
                "img": IMG, "arch": "mobilenet_v3_small"}, os.path.join(OUT, "best_model.pt"))
    # write charset + maps
    open(os.path.join(OUT,"charset.txt"),"w").write(CHARSET+"\n")
    json.dump({c:i for i,c in enumerate(CHARSET)}, open(os.path.join(OUT,"label_to_index.json"),"w"), indent=2)
    json.dump({str(i):c for i,c in enumerate(CHARSET)}, open(os.path.join(OUT,"index_to_label.json"),"w"), indent=2)
    with open(os.path.join(OUT,"training_history.csv"),"w",newline='') as f:
        w=csv.DictWriter(f,fieldnames=["epoch","val_macro_f1","val_acc"]); w.writeheader(); w.writerows(hist)
    print(f"BEST val_macro_f1={best_f1:.4f}; saved best_model.pt")
    # stash for eval script
    json.dump({"best_val_macro_f1": round(best_f1,4), "img": IMG, "nclass": NCLASS,
               "arch":"mobilenet_v3_small","seed":SEED,
               "counts":{"train":len(tr),"val":len(va),"test":len(te)}},
              open(os.path.join(OUT,"_train_stub.json"),"w"), indent=2)

if __name__ == "__main__":
    main()
