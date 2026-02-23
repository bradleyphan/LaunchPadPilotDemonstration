import os
from flask import Flask, request, jsonify, render_template
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "launchpad")]
profiles_collection = db["profiles"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/add_profile", methods=["POST"])
def add_profile():
    data = request.get_json()

    name = data.get("name", "").strip()
    loan_balance = data.get("loan_balance")
    starting_salary = data.get("starting_salary")

    if not name or loan_balance is None or starting_salary is None:
        return jsonify({"error": "All fields are required."}), 400

    try:
        loan_balance = float(loan_balance)
        starting_salary = float(starting_salary)
    except (ValueError, TypeError):
        return jsonify({"error": "Loan balance and salary must be valid numbers."}), 400

    profile = {
        "name": name,
        "loan_balance": loan_balance,
        "starting_salary": starting_salary,
        "debt_to_income_ratio": round(loan_balance / starting_salary, 4) if starting_salary > 0 else None,
        "created_at": datetime.utcnow().isoformat(),
    }

    result = profiles_collection.insert_one(profile)
    profile["_id"] = str(result.inserted_id)

    return jsonify({"message": "Profile saved successfully.", "profile": profile}), 201


@app.route("/get_profiles", methods=["GET"])
def get_profiles():
    profiles = list(profiles_collection.find().sort("created_at", -1))
    for p in profiles:
        p["_id"] = str(p["_id"])
    return jsonify(profiles), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
