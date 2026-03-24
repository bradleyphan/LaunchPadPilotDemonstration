import os
from functools import wraps
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from dotenv import load_dotenv
from datetime import datetime
import bcrypt

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "launchpad-dev-secret-change-in-prod")

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "launchpad")]
users_col     = db["users"]
profiles_col  = db["profiles"]
roadmap_col   = db["roadmap"]
snapshots_col = db["snapshots"]

users_col.create_index("email", unique=True)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Financial calculation engine ─────────────────────────────────────────────

def life_path_projections(salary, loan_balance, loan_rate_pct, monthly_expenses, months=60):
    monthly_income = salary / 12
    surplus = monthly_income - monthly_expenses
    monthly_loan_rate = (loan_rate_pct / 100) / 12
    hysa_rate = 0.045 / 12
    roth_rate = 0.07 / 12

    if loan_balance > 0 and monthly_loan_rate > 0:
        n = 120
        r = monthly_loan_rate
        min_payment = max(100, loan_balance * (r * (1 + r) ** n) / ((1 + r) ** n - 1))
    else:
        min_payment = 0

    states = {
        "aggressive": {"loan": float(loan_balance), "inv": 0.0},
        "balanced":   {"loan": float(loan_balance), "inv": 0.0},
        "investor":   {"loan": float(loan_balance), "inv": 0.0},
    }
    paths = {
        "aggressive": {"label": "Aggressive Payoff", "color": "#0f62fe", "data": []},
        "balanced":   {"label": "Balanced",           "color": "#6929c4", "data": []},
        "investor":   {"label": "Investor Path",      "color": "#24a148", "data": []},
    }

    for _ in range(months):
        s = states["aggressive"]
        if s["loan"] > 0:
            s["loan"] = max(0, s["loan"] * (1 + monthly_loan_rate) - surplus)
            s["inv"] *= (1 + hysa_rate)
        else:
            s["inv"] = (s["inv"] + surplus) * (1 + hysa_rate)
        paths["aggressive"]["data"].append(round(s["inv"] - s["loan"], 2))

        s = states["balanced"]
        if s["loan"] > 0:
            extra = max(0, surplus - min_payment) / 2
            s["loan"] = max(0, s["loan"] * (1 + monthly_loan_rate) - (min_payment + extra))
            s["inv"] = (s["inv"] + extra) * (1 + roth_rate)
        else:
            s["inv"] = (s["inv"] + surplus) * (1 + roth_rate)
        paths["balanced"]["data"].append(round(s["inv"] - s["loan"], 2))

        s = states["investor"]
        if s["loan"] > 0:
            pmt = min(min_payment, s["loan"] * (1 + monthly_loan_rate))
            s["loan"] = max(0, s["loan"] * (1 + monthly_loan_rate) - pmt)
            s["inv"] = (s["inv"] + max(0, surplus - pmt)) * (1 + roth_rate)
        else:
            s["inv"] = (s["inv"] + surplus) * (1 + roth_rate)
        paths["investor"]["data"].append(round(s["inv"] - s["loan"], 2))

    return paths


def paycheck_allocation(salary):
    mo = salary / 12
    return {
        "monthly":    round(mo, 2),
        "needs":      round(mo * 0.50, 2),
        "debt":       round(mo * 0.20, 2),
        "retirement": round(mo * 0.10, 2),
        "lifestyle":  round(mo * 0.20, 2),
    }


def match_401k_analysis(salary, match_pct):
    monthly_gross  = salary / 12
    user_monthly   = monthly_gross * match_pct / 100
    employer_monthly = user_monthly  # dollar-for-dollar up to match_pct
    total_monthly  = user_monthly + employer_monthly
    growth_rate    = 0.07 / 12

    projections = {}
    for years in [5, 10, 20, 30]:
        months = years * 12
        with_match, without_match = 0.0, 0.0
        for _ in range(months):
            with_match    = (with_match    + total_monthly)  * (1 + growth_rate)
            without_match = (without_match + user_monthly)   * (1 + growth_rate)
        projections[str(years)] = {
            "with_match":       round(with_match, 0),
            "without_match":    round(without_match, 0),
            "free_money_value": round(with_match - without_match, 0),
        }

    return {
        "salary":            round(salary, 2),
        "match_pct":         match_pct,
        "user_monthly":      round(user_monthly, 2),
        "employer_monthly":  round(employer_monthly, 2),
        "total_monthly":     round(total_monthly, 2),
        "annual_free":       round(employer_monthly * 12, 2),
        "projections":       projections,
        "irs_limit":         23500,
        "maxing_out":        (user_monthly * 12) >= 23500,
    }


def rent_affordability(salary, loan_balance, loan_rate_pct, current_rent=0):
    monthly_gross = salary / 12
    # Approximate take-home after federal + state taxes (~22% effective rate for $50-80k)
    monthly_takehome = monthly_gross * 0.78

    r = (loan_rate_pct / 100) / 12
    if loan_balance > 0 and r > 0:
        n = 120
        min_payment = max(100, loan_balance * (r * (1 + r) ** n) / ((1 + r) ** n - 1))
    else:
        min_payment = 0

    tiers = []
    for key, pct, label in [
        ("comfortable", 0.25, "Comfortable"),
        ("standard",    0.28, "Standard Max"),
        ("stretch",     0.33, "Stretch"),
    ]:
        rent = monthly_gross * pct
        after_rent = monthly_takehome - rent
        after_loan = after_rent - min_payment
        # Savings potential = 20% of what's left after rent + loan
        savings = max(0, after_loan * 0.20)
        emergency_target = rent * 3  # 3 months of rent as a proxy
        months_to_emergency = round(emergency_target / savings) if savings > 0 else 99
        verdict = "good" if pct <= 0.25 else ("ok" if pct <= 0.28 else "tight")

        tiers.append({
            "key":               key,
            "label":             label,
            "rent":              round(rent, 0),
            "pct_gross":         round(pct * 100, 0),
            "after_rent":        round(after_rent, 0),
            "after_loan":        round(max(0, after_loan), 0),
            "savings_potential": round(savings, 0),
            "months_to_emergency": min(int(months_to_emergency), 99),
            "verdict":           verdict,
        })

    return {
        "monthly_gross":     round(monthly_gross, 0),
        "monthly_takehome":  round(monthly_takehome, 0),
        "loan_min_payment":  round(min_payment, 0),
        "current_rent":      round(current_rent, 0),
        "tiers":             tiers,
    }


def generate_advice(profile):
    salary = profile.get("salary", 0)
    loan = profile.get("loan_balance", 0)
    expenses = profile.get("monthly_expenses", 0)
    match_pct = profile.get("employer_401k_match", 0)
    signing = profile.get("signing_bonus", 0)
    monthly = salary / 12
    dti = loan / salary if salary > 0 else 0
    surplus = monthly - expenses
    emergency_target = expenses * 3

    advice = [{
        "icon": "shield-check-fill",
        "title": "Build Your Emergency Fund First",
        "body": f"Before extra debt payments or investing, save ${emergency_target:,.0f} (3 months of expenses) in a high-yield savings account earning ~4.5% APY. This is your financial safety net.",
        "priority": "high"
    }]

    if dti < 0.15:
        advice.append({
            "icon": "graph-up-arrow",
            "title": "Strong DTI — Shift Focus to Wealth Building",
            "body": f"Your {dti:.0%} debt-to-income ratio is excellent. Max out your Roth IRA ($7,000/yr limit) before aggressively paying down low-interest student debt.",
            "priority": "high"
        })
    elif dti < 0.35:
        advice.append({
            "icon": "arrow-left-right",
            "title": "Balanced DTI — Split Your Surplus Strategically",
            "body": f"At {dti:.0%} DTI, you're in a good position. Run the Balanced Path: split your ${surplus:,.0f}/month surplus between extra loan payments and Roth IRA contributions.",
            "priority": "medium"
        })
    else:
        advice.append({
            "icon": "exclamation-triangle-fill",
            "title": "High DTI — Debt Reduction is Priority One",
            "body": f"A {dti:.0%} DTI limits your financial options. Focus on the Aggressive Payoff path to eliminate your ${loan:,.0f} debt faster, then redirect that payment to investing.",
            "priority": "high"
        })

    if match_pct > 0:
        free_money = salary * match_pct / 100
        advice.append({
            "icon": "currency-dollar",
            "title": f"Capture ${free_money:,.0f}/yr in Free 401k Money",
            "body": f"Contribute at least {match_pct}% (${free_money/12:,.0f}/mo) to your 401k to get the full employer match. This is a guaranteed 100% return — always do this before anything else.",
            "priority": "high"
        })

    if signing > 0:
        if dti > 0.2:
            advice.append({
                "icon": "gift-fill",
                "title": f"Deploy Your ${signing:,.0f} Signing Bonus Wisely",
                "body": f"With your current DTI, put 60% (${signing*0.6:,.0f}) toward your student loan immediately — this saves real interest. Keep 30% (${signing*0.3:,.0f}) as your emergency fund starter and 10% for yourself.",
                "priority": "medium"
            })
        else:
            advice.append({
                "icon": "gift-fill",
                "title": f"Invest Your ${signing:,.0f} Signing Bonus",
                "body": f"Your DTI is healthy. Put up to $7,000 into a Roth IRA and the rest into HYSA. Investing a lump sum early maximizes compounding — don't spend it on lifestyle upgrades.",
                "priority": "medium"
            })

    if surplus < 500:
        advice.append({
            "icon": "wallet2",
            "title": "Tight Cash Flow — Audit Your Monthly Expenses",
            "body": f"Only ${surplus:,.0f}/month in surplus leaves little margin. Review your top 3 expenses (rent, subscriptions, food). Cutting $200/month invested for 30 years at 7% = ~$24,000.",
            "priority": "medium"
        })

    return advice


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data = request.get_json()
        name = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")

        if not name or not email or not password:
            return jsonify({"error": "All fields are required."}), 400
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters."}), 400

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        try:
            result = users_col.insert_one({
                "name": name,
                "email": email,
                "password": pw_hash,
                "created_at": datetime.utcnow().isoformat(),
            })
            session["user_id"] = str(result.inserted_id)
            session["user_name"] = name
            return jsonify({"redirect": url_for("onboard")}), 200
        except DuplicateKeyError:
            return jsonify({"error": "An account with that email already exists."}), 409

    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        user = users_col.find_one({"email": email})

        if not user or not bcrypt.checkpw(password.encode(), user["password"].encode()):
            return jsonify({"error": "Invalid email or password."}), 401

        session["user_id"] = str(user["_id"])
        session["user_name"] = user["name"]
        profile = profiles_col.find_one({"user_id": str(user["_id"])})
        dest = url_for("dashboard") if profile else url_for("onboard")
        return jsonify({"redirect": dest}), 200

    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/onboard")
@require_auth
def onboard():
    return render_template("onboard.html")


@app.route("/dashboard")
@require_auth
def dashboard():
    profile = profiles_col.find_one({"user_id": session["user_id"]})
    if not profile:
        return redirect(url_for("onboard"))
    return render_template("dashboard.html", user_name=session.get("user_name", ""))


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/profile", methods=["GET", "POST"])
@require_auth
def api_profile():
    if request.method == "POST":
        data = request.get_json()
        monthly_rent   = float(data.get("monthly_rent",   0))
        other_expenses = float(data.get("other_expenses", 0))
        profile = {
            "user_id":             session["user_id"],
            "salary":              float(data.get("salary", 0)),
            "loan_balance":        float(data.get("loan_balance", 0)),
            "loan_rate":           float(data.get("loan_rate", 5.5)),
            "monthly_rent":        monthly_rent,
            "other_expenses":      other_expenses,
            "monthly_expenses":    monthly_rent + other_expenses,
            "employer_401k_match": float(data.get("employer_401k_match", 0)),
            "signing_bonus":       float(data.get("signing_bonus", 0)),
            "updated_at":          datetime.utcnow().isoformat(),
        }
        profiles_col.update_one({"user_id": session["user_id"]}, {"$set": profile}, upsert=True)
        return jsonify({"redirect": url_for("dashboard")}), 200

    profile = profiles_col.find_one({"user_id": session["user_id"]}, {"_id": 0})
    return (jsonify(profile), 200) if profile else (jsonify({}), 404)


@app.route("/api/life-paths")
@require_auth
def api_life_paths():
    p = profiles_col.find_one({"user_id": session["user_id"]})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(life_path_projections(p["salary"], p["loan_balance"], p.get("loan_rate", 5.5), p["monthly_expenses"])), 200


@app.route("/api/paycheck")
@require_auth
def api_paycheck():
    p = profiles_col.find_one({"user_id": session["user_id"]})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(paycheck_allocation(p["salary"])), 200


@app.route("/api/rent")
@require_auth
def api_rent():
    p = profiles_col.find_one({"user_id": session["user_id"]})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(rent_affordability(p["salary"], p["loan_balance"], p.get("loan_rate", 5.5), p.get("monthly_rent", 0))), 200


@app.route("/api/advice")
@require_auth
def api_advice():
    p = profiles_col.find_one({"user_id": session["user_id"]}, {"_id": 0})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(generate_advice(p)), 200


@app.route("/api/roadmap", methods=["GET", "POST"])
@require_auth
def api_roadmap():
    if request.method == "POST":
        data = request.get_json()
        task_id = data.get("task_id")
        op = "$addToSet" if data.get("completed", True) else "$pull"
        roadmap_col.update_one({"user_id": session["user_id"]}, {op: {"completed": task_id}}, upsert=True)
        return jsonify({"ok": True}), 200

    doc = roadmap_col.find_one({"user_id": session["user_id"]})
    return jsonify({"completed": doc.get("completed", []) if doc else []}), 200


@app.route("/api/401k")
@require_auth
def api_401k():
    p = profiles_col.find_one({"user_id": session["user_id"]})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(match_401k_analysis(p["salary"], p.get("employer_401k_match", 0))), 200


@app.route("/api/snapshots", methods=["GET", "POST"])
@require_auth
def api_snapshots():
    if request.method == "POST":
        data     = request.get_json()
        date     = data.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
        loan     = float(data.get("loan_balance", 0))
        savings  = float(data.get("savings_balance", 0))
        snapshots_col.update_one(
            {"user_id": session["user_id"], "date": date},
            {"$set": {
                "user_id":         session["user_id"],
                "date":            date,
                "loan_balance":    round(loan, 2),
                "savings_balance": round(savings, 2),
                "net_worth":       round(savings - loan, 2),
            }},
            upsert=True
        )
        return jsonify({"ok": True}), 200

    docs = list(snapshots_col.find(
        {"user_id": session["user_id"]},
        {"_id": 0, "user_id": 0}
    ).sort("date", 1))
    return jsonify(docs), 200


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
