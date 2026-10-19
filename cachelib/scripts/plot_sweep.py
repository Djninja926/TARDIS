import sys, pandas as pd, numpy as np
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
CSV = sys.argv[1] if len(sys.argv) > 1 else "/mydata/tardis/sweep_results.csv"
KNEE_THRESH_PP = 1.0
df = pd.read_csv(CSV).dropna(subset=["miss_ratio"])
df["miss_ratio"] = df["miss_ratio"].astype(float)
df["cache_mb"] = df["cache_mb"].astype(int)
traces = sorted(df["trace"].unique())
algos = ["lru","s3fifo","lruforgive"]
lab = {"lru":"LRU","s3fifo":"S3FIFO","lruforgive":"LRU-TARDIS"}
col = {"lru":"#1f77b4","s3fifo":"#2ca02c","lruforgive":"#d62728"}
def knee(s, m):
    o = np.argsort(s); s=np.array(s)[o]; m=np.array(m)[o]
    for i in range(1,len(s)):
        if (m[i-1]-m[i])*100 < KNEE_THRESH_PP: return int(s[i-1]), float(m[i-1])
    return int(s[-1]), float(m[-1])
n=len(traces); cols=2; rows=(n+cols-1)//cols
fig,axes=plt.subplots(rows,cols,figsize=(13,4.2*rows),squeeze=False)
for idx,tr in enumerate(traces):
    ax=axes[idx//cols][idx%cols]; sub=df[df.trace==tr]
    for a in algos:
        d=sub[sub.algo==a].sort_values("cache_mb")
        if d.empty: continue
        ax.plot(d.cache_mb,d.miss_ratio,marker="o",label=lab[a],color=col[a])
        if a=="lru":
            kmb,km=knee(d.cache_mb.values,d.miss_ratio.values)
            ax.axvline(kmb,color="gray",ls="--",alpha=0.6)
            ax.annotate(f"LRU knee {kmb}MB",(kmb,km),textcoords="offset points",xytext=(8,8),fontsize=8)
    ax.set_xscale("log",base=2); ax.set_title(tr,fontsize=10)
    ax.set_xlabel("cache MB (log2)"); ax.set_ylabel("miss ratio")
    ax.grid(True,alpha=0.3); ax.legend(fontsize=8)
for j in range(n,rows*cols): axes[j//cols][j%cols].axis("off")
plt.tight_layout(); plt.savefig("/mydata/tardis/sweep_plot.png",dpi=130,bbox_inches="tight")
print("Saved -> /mydata/tardis/sweep_plot.png")
print("\n===== TARDIS gap vs LRU / S3FIFO (pp) =====")
for tr in traces:
    sub=df[df.trace==tr]; piv=sub.pivot_table(index="cache_mb",columns="algo",values="miss_ratio")
    if not {"lru","lruforgive"}.issubset(piv.columns): continue
    print(f"\n{tr}")
    print(f"  {'MB':>6} {'LRU':>8} {'S3FIFO':>8} {'TARDIS':>8} {'T-LRU':>8} {'T-S3F':>8}")
    for mb,row in piv.sort_index().iterrows():
        l=row.get("lru",np.nan); s=row.get("s3fifo",np.nan); t=row.get("lruforgive",np.nan)
        gl=(l-t)*100 if not np.isnan(l) and not np.isnan(t) else np.nan
        gs=(s-t)*100 if not np.isnan(s) and not np.isnan(t) else np.nan
        print(f"  {mb:>6} {l:>8.4f} {s:>8.4f} {t:>8.4f} {gl:>7.2f}p {gs:>7.2f}p")
