import os
from functools import wraps
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
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



def paycheck_allocation(salary, loan_balance=0, loan_rate_pct=5.5, monthly_expenses=0, employer_401k_match=0):
    mo = round(salary / 12, 2)
    r  = (loan_rate_pct / 100) / 12
    if loan_balance > 0 and r > 0:
        n           = 120
        min_payment = max(100, loan_balance * (r * (1 + r) ** n) / ((1 + r) ** n - 1))
    else:
        min_payment = 0

    needs       = round(monthly_expenses if monthly_expenses > 0 else mo * 0.50, 2)
    debt        = round(min_payment, 2)
    retire_pct  = employer_401k_match / 100 if employer_401k_match > 0 else 0.10
    retirement  = round(mo * retire_pct, 2)
    lifestyle   = round(max(0, mo - needs - debt - retirement), 2)
    return {
        "monthly":    mo,
        "needs":      needs,
        "debt":       debt,
        "retirement": retirement,
        "lifestyle":  lifestyle,
        "retire_pct": round(retire_pct * 100, 1),
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
        "irs_max_pct":       round(min(100, (23500 / salary) * 100), 1) if salary > 0 else 100,
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



@app.route("/api/paycheck")
@require_auth
def api_paycheck():
    p = profiles_col.find_one({"user_id": session["user_id"]})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(paycheck_allocation(
        p["salary"],
        p.get("loan_balance", 0),
        p.get("loan_rate", 5.5),
        p.get("monthly_expenses", 0),
        p.get("employer_401k_match", 0)
    )), 200


@app.route("/api/rent")
@require_auth
def api_rent():
    p = profiles_col.find_one({"user_id": session["user_id"]})
    if not p:
        return jsonify({"error": "No profile"}), 404
    return jsonify(rent_affordability(p["salary"], p["loan_balance"], p.get("loan_rate", 5.5), p.get("monthly_rent", 0))), 200



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


@app.route("/api/savings-goal", methods=["GET", "POST"])
@require_auth
def api_savings_goal():
    if request.method == "POST":
        data = request.get_json()
        profiles_col.update_one(
            {"user_id": session["user_id"]},
            {"$set": {
                "savings_goal_amount": float(data.get("amount", 0)),
                "savings_goal_date":   data.get("date", ""),
            }},
            upsert=True
        )
        return jsonify({"ok": True}), 200
    p = profiles_col.find_one({"user_id": session["user_id"]}, {"_id": 0})
    return jsonify({
        "amount": p.get("savings_goal_amount", 0) if p else 0,
        "date":   p.get("savings_goal_date",   "") if p else "",
    }), 200


@app.route("/api/snapshots/<date>", methods=["DELETE"])
@require_auth
def delete_snapshot(date):
    snapshots_col.delete_one({"user_id": session["user_id"], "date": date})
    return jsonify({"ok": True}), 200


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
