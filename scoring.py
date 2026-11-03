"""ML lead scoring: features, training (compare 3 models, calibrate), serving.
Train:  python scoring.py train            (synthetic bootstrap data)
        python scoring.py train leads.csv  (real data; needs column 'won' = 0/1 + feature columns)
"""
import sys, json, hashlib, datetime as dt
from pathlib import Path
import numpy as np, pandas as pd, joblib
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, average_precision_score

MODELS = Path(__file__).parent / "models"
CAT = ["service_type", "source"]
NUM = ["budget_usd", "urgency_num", "desc_len", "n_features", "has_phone",
       "corporate_email", "timeline_days", "has_company"]
SERVICES = ["web development", "mobile app", "custom software", "AI/automation",
            "UI/UX design", "maintenance/support", "consulting", "other"]
SOURCES = ["web_form", "email", "referral", "manual"]
TIERS = {"Hot": 70, "Warm": 40}  # configurable thresholds


def synthetic(n=2000, seed=7):
    r = np.random.default_rng(seed)
    d = pd.DataFrame({
        "service_type": r.choice(SERVICES, n, p=[.25, .18, .15, .15, .08, .08, .06, .05]),
        "source": r.choice(SOURCES, n, p=[.5, .25, .15, .1]),
        "budget_usd": np.where(r.random(n) < .25, 0, r.lognormal(8, 1.1, n)).round(),
        "urgency_num": r.integers(0, 4, n), "desc_len": r.gamma(3, 120, n).round(),
        "n_features": r.poisson(3, n), "has_phone": r.integers(0, 2, n),
        "corporate_email": (r.random(n) < .55).astype(int),
        "timeline_days": r.choice([14, 30, 60, 90, 180, 0], n), "has_company": (r.random(n) < .6).astype(int)})
    z = (-2.2 + 0.00025 * d.budget_usd.clip(upper=15000) + 0.0012 * d.desc_len.clip(upper=800)
         + 0.25 * d.n_features + 0.7 * d.corporate_email + 0.4 * d.has_phone + 0.5 * d.has_company
         + 0.6 * (d.source == "referral") + 0.25 * d.urgency_num - 0.8 * (d.budget_usd == 0)
         + r.normal(0, 1.0, n))
    d["won"] = (r.random(n) < 1 / (1 + np.exp(-z))).astype(int)
    return d


def _pre():
    return ColumnTransformer([("c", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CAT),
                              ("n", StandardScaler(), NUM)])


def train(csv=None):
    synthetic_flag = csv is None
    df = synthetic() if synthetic_flag else pd.read_csv(csv)
    X, y = df[CAT + NUM], df["won"]
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=.25, stratify=y, random_state=1)
    cands = {"logreg": LogisticRegression(max_iter=1000, class_weight="balanced"),
             "random_forest": RandomForestClassifier(300, min_samples_leaf=5, class_weight="balanced", random_state=1),
             "hist_gb": HistGradientBoostingClassifier(max_depth=3, learning_rate=.05, random_state=1)}
    res, best, best_auc = {}, None, -1
    for name, m in cands.items():
        cal = CalibratedClassifierCV(Pipeline([("p", _pre()), ("m", m)]), cv=3, method="sigmoid").fit(Xtr, ytr)
        pr = cal.predict_proba(Xte)[:, 1]
        res[name] = {"roc_auc": round(roc_auc_score(yte, pr), 4), "pr_auc": round(average_precision_score(yte, pr), 4)}
        if res[name]["roc_auc"] > best_auc: best, best_auc = (name, cal), res[name]["roc_auc"]
    MODELS.mkdir(exist_ok=True)
    ver = "v" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    meta = {"version": ver, "best": best[0], "metrics": res, "trained_at": dt.datetime.now().isoformat(),
            "dataset_hash": hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()[:16],
            "synthetic_data": synthetic_flag, "rows": len(df),
            "baseline": {**{c: X[c].mode()[0] for c in CAT}, **{c: float(X[c].median()) for c in NUM}},
            "note": "SYNTHETIC data - metrics are NOT real-world performance." if synthetic_flag else "Real data."}
    joblib.dump(best[1], MODELS / f"model_{ver}.joblib")
    (MODELS / f"meta_{ver}.json").write_text(json.dumps(meta, indent=2))
    (MODELS / "active.json").write_text(json.dumps({"version": ver}))  # roll back by editing this file
    print(json.dumps(meta, indent=2))


_cache = {}
def _load():
    ver = json.loads((MODELS / "active.json").read_text())["version"]
    if _cache.get("v") != ver:
        _cache.update(v=ver, model=joblib.load(MODELS / f"model_{ver}.joblib"),
                      meta=json.loads((MODELS / f"meta_{ver}.json").read_text()))
    return _cache["model"], _cache["meta"]


def tier_for(score):
    return "Hot" if score >= TIERS["Hot"] else "Warm" if score >= TIERS["Warm"] else "Cold"


def score(f: dict):
    """f: feature dict -> {score, tier, probability, top_factors, model_version}"""
    model, meta = _load()
    row = pd.DataFrame([{k: f.get(k, meta["baseline"][k]) for k in CAT + NUM}])
    p = float(model.predict_proba(row)[0, 1])
    deltas = []
    for c in CAT + NUM:  # per-lead explanation: swap feature for baseline, see the change
        alt = row.copy(); alt[c] = meta["baseline"][c]
        deltas.append((c, p - float(model.predict_proba(alt)[0, 1])))
    top = [{"feature": c, "value": row[c].iloc[0].item() if hasattr(row[c].iloc[0], "item") else row[c].iloc[0],
            "impact_points": round(d * 100, 1)} for c, d in sorted(deltas, key=lambda x: -abs(x[1]))[:3]]
    s = round(p * 100)
    return {"score": s, "tier": tier_for(s), "probability": round(p, 3), "top_factors": top, "model_version": meta["version"]}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "train": train(sys.argv[2] if len(sys.argv) > 2 else None)
    else: print(__doc__)
