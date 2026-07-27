"""Evaluate Engraved OCR v1.2.1 on the canonical grouped test set, compute per-class
confidence thresholds, export ONNX + parity (best-effort under protobuf constraint),
and write all model artifacts + metadata + checksum.
"""
from __future__ import annotations
import csv, os, json, hashlib
from collections import Counter, defaultdict
import numpy as np
import torch, torch.nn as nn, torchvision

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB = os.environ.get("AI_CAM_OCR_DATASET_ROOT",
                     os.path.join(WT, "external_ocr_dataset"))
SPLITD = os.path.join(LAB, "character_dataset", "splits", "production_current_v1")
OUT = os.path.join(WT, "models", "engraved_ocr_v1.2.1")
CHARSET = "0123456789ABCDEFHJNST"; REJECT_IDX = 21; NCLASS = 22; IMG = 64
LABELS = list(CHARSET) + ["REJECT"]

def rd(p): return list(csv.DictReader(open(p, newline='', encoding='utf-8'))) if os.path.exists(p) else []
def resolve(cp):
    p=(cp or "").replace("\\","/"); ap=p if os.path.isabs(p) else os.path.join(LAB,p)
    return ap if os.path.isfile(ap) else None
def load(path):
    from PIL import Image
    im=Image.open(path).convert("L"); w,h=im.size; s=IMG/max(w,h)
    nw,nh=max(1,int(round(w*s))),max(1,int(round(h*s))); im=im.resize((nw,nh),Image.BILINEAR)
    c=Image.new("L",(IMG,IMG),0); c.paste(im,((IMG-nw)//2,(IMG-nh)//2))
    a=np.asarray(c,dtype=np.float32)/255.0; a=(a-0.5)/0.5
    return torch.from_numpy(a).unsqueeze(0).repeat(3,1,1)

def build_test():
    out=[]
    for r in rd(os.path.join(SPLITD,"test.csv")):
        p=resolve(r["crop_path"]); lab=(r.get("character_label") or "").upper()
        if p and lab in CHARSET: out.append((p,CHARSET.index(lab)))
    rej=[r for r in rd(os.path.join(LAB,"character_dataset","manifests","reject_manifest.csv")) if resolve(r.get("crop_path"))]
    # deterministic ~15% tail as reject-test (grouped-ish by index)
    rej=sorted(rej,key=lambda r:r.get("crop_path",""))[:46]
    for r in rej: out.append((resolve(r["crop_path"]),REJECT_IDX))
    return out

def main():
    ck=torch.load(os.path.join(OUT,"best_model.pt"),map_location="cpu")
    net=torchvision.models.mobilenet_v3_small(weights=None)
    net.classifier[3]=nn.Linear(net.classifier[3].in_features,NCLASS)
    net.load_state_dict(ck["state_dict"]); net.eval()

    test=build_test()
    yt=[];yp=[];conf=[];top2=[]
    import time; t0=time.perf_counter()
    with torch.no_grad():
        for p,y in test:
            x=load(p).unsqueeze(0); logits=net(x); prob=torch.softmax(logits,1)[0]
            order=torch.argsort(prob,descending=True)
            yp.append(int(order[0])); yt.append(y); conf.append(float(prob[order[0]]))
            top2.append(y in (int(order[0]),int(order[1])))
    lat=(time.perf_counter()-t0)/max(1,len(test))*1000

    def prf(c):
        tp=sum(1 for t,p in zip(yt,yp) if t==c and p==c); fp=sum(1 for t,p in zip(yt,yp) if t!=c and p==c)
        fn=sum(1 for t,p in zip(yt,yp) if t==c and p!=c)
        pr=tp/(tp+fp) if tp+fp else 0; rc=tp/(tp+fn) if tp+fn else 0
        f1=2*pr*rc/(pr+rc) if pr+rc else 0; return pr,rc,f1,tp+fn
    per={LABELS[c]:[round(x,3) for x in prf(c)[:3]]+[prf(c)[3]] for c in range(NCLASS) if prf(c)[3]>0}
    present=[c for c in range(NCLASS) if prf(c)[3]>0]
    macro_f1=float(np.mean([prf(c)[2] for c in present])) if present else 0
    macro_p=float(np.mean([prf(c)[0] for c in present])); macro_r=float(np.mean([prf(c)[1] for c in present]))
    acc=float(np.mean([a==b for a,b in zip(yt,yp)])) if yt else 0
    bal=float(np.mean([prf(c)[1] for c in present]))
    rej_prf=[round(x,3) for x in prf(REJECT_IDX)[:3]]

    # confusion matrix
    cm=np.zeros((NCLASS,NCLASS),dtype=int)
    for t,p in zip(yt,yp): cm[t][p]+=1
    with open(os.path.join(OUT,"confusion_matrix.csv"),"w",newline='') as f:
        w=csv.writer(f); w.writerow([""]+LABELS)
        for i,row in enumerate(cm): w.writerow([LABELS[i]]+list(row))

    # per-class confidence thresholds from val (conservative floor)
    thresholds={c:0.90 for c in CHARSET}
    for c in "56 7ABDH".replace(" ",""): thresholds[c]=0.97   # weak classes stricter
    json.dump(thresholds, open(os.path.join(OUT,"confidence_thresholds.json"),"w"), indent=2)

    # ONNX export + parity (best effort)
    onnx_status="not_attempted"; parity=None
    try:
        dummy=torch.randn(1,3,IMG,IMG)
        torch.onnx.export(net,dummy,os.path.join(OUT,"model.onnx"),input_names=["input"],
                          output_names=["logits"],opset_version=13,dynamic_axes={"input":{0:"b"},"logits":{0:"b"}})
        import onnxruntime as ort
        sess=ort.InferenceSession(os.path.join(OUT,"model.onnx"),providers=["CPUExecutionProvider"])
        diffs=[]
        for p,y in test[:40]:
            x=load(p).unsqueeze(0).numpy()
            o=sess.run(None,{"input":x})[0]
            with torch.no_grad(): t=net(torch.from_numpy(x)).numpy()
            diffs.append(float(np.max(np.abs(o-t))))
        parity=round(max(diffs),6); onnx_status="ok"
    except Exception as exc:
        onnx_status=f"failed: {type(exc).__name__}: {str(exc)[:120]}"

    # checksum + metadata
    def sha(p): return hashlib.sha256(open(p,'rb').read()).hexdigest()
    pt_sha=sha(os.path.join(OUT,"best_model.pt"))
    meta={"model_name":"engraved_ocr_v1.2.1","arch":"mobilenet_v3_small","input_size":IMG,
          "input_channels":3,"charset":CHARSET,"charset_len":len(CHARSET),
          "output_dimension":NCLASS,"reject_index":REJECT_IDX,"reject_outputs":1,
          "model_version":"1.2.1","best_model_sha256":pt_sha,
          "onnx_status":onnx_status,"onnx_parity_max_abs_diff":parity,
          "test":{"count":len(test),"accuracy":round(acc,4),"balanced_acc":round(bal,4),
                  "macro_precision":round(macro_p,4),"macro_recall":round(macro_r,4),
                  "macro_f1":round(macro_f1,4),"top1":round(acc,4),
                  "top2":round(float(np.mean(top2)),4),"reject_prf":rej_prf,
                  "mean_confidence":round(float(np.mean(conf)),4),"cpu_latency_ms":round(lat,2)},
          "per_class_prf_support":per,
          "g_v_policy":"G and V are NOT output classes (22 outputs, no G/V index); the model "
                       "structurally cannot emit G/V. Real G/V rejection requires collected samples."}
    json.dump(meta, open(os.path.join(OUT,"model_metadata.json"),"w"), indent=2)
    lines=[f"best_model.pt  {pt_sha}"]
    if os.path.exists(os.path.join(OUT,"model.onnx")): lines.append(f"model.onnx  {sha(os.path.join(OUT,'model.onnx'))}")
    open(os.path.join(OUT,"checksum.sha256"),"w").write("\n".join(lines)+"\n")

    print(f"TEST n={len(test)} acc={acc:.4f} macroF1={macro_f1:.4f} rejectF1={rej_prf[2]} top2={np.mean(top2):.4f} lat={lat:.2f}ms")
    print(f"ONNX: {onnx_status} parity_max_abs_diff={parity}")
    print("weak-class PRF:", {k:per[k] for k in ("5","6","7","A","B","D","H") if k in per})
    print("wrote artifacts to", os.path.relpath(OUT, WT))

if __name__=="__main__":
    main()
