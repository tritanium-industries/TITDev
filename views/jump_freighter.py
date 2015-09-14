import os
import json
from operator import itemgetter

from flask import Blueprint, render_template, g, request, session
from bson.objectid import ObjectId

from helpers import caches, conversions
from views.auth import requires_sso, auth_check

jf = Blueprint("jf", __name__, template_folder="templates")

if os.environ.get("HEROKU"):
    secrets = {
        "jf_key_id": os.environ["jf_key_id"],
        "jf_vcode": os.environ["jf_vcode"]
    }
else:
    with open("../Other-Secrets/TITDev.json") as secrets_file:
        secrets = json.load(secrets_file)


def validator(start_station, end_station, contract):
    # Check if contract is valid
    validation_query = g.mongo.db.jfroutes.find_one({
        "start": start_station.strip(),
        "end": end_station.strip()})
    validation_failed = ["Failed", "Cancelled", "Rejected", "Reversed", "Deleted"]
    validation_completed = ["Completed", "CompletedByIssuer", "CompletedByContractor"]
    color = ""  # Default color, used for outstanding

    if validation_query:
        validation_calc = max(validation_query["m3"] * contract["volume"] +
                              validation_query["extra"] + contract["collateral"] * 0.1, 1000000)
        if contract["reward"] < validation_calc or contract["volume"] > 300000:
            color = "info"
    else:
        color = "active"

    if contract["status"] in validation_failed:
        color = "danger"
    elif contract["status"] == "InProgress":
        color = "warning"
    elif contract["status"] in validation_completed:
        color = "success"

    return color


@jf.route("/")
@requires_sso("alliance")
def home():
    start_list = []
    end_list = []
    for station in g.mongo.db.jfroutes.distinct("start"):
        if request.args.get("start") == station:
            start_list.append([station, True])
        else:
            start_list.append([station, False])
    for station in g.mongo.db.jfroutes.distinct("end"):
        if request.args.get("end") == station:
            end_list.append([station, True])
        else:
            end_list.append([station, False])
    start_list.sort()
    end_list.sort()

    # Contract Calculations
    if request.args.get("start") and request.args.get("end"):
        selected_route = g.mongo.db.jfroutes.find_one({"start": request.args.get("start"),
                                                       "end": request.args.get("end")})
        if selected_route:
            m3 = selected_route["m3"]
            extra = selected_route["extra"]

            volume = request.args.get("volume")
            collateral = request.args.get("collateral")
            volume = float(volume) if volume else 0
            collateral = float(collateral) if collateral else 0

            volume_cost = m3 * volume
            collateral_cost = collateral * 0.1
            price = extra + volume_cost + collateral_cost

            # Mark Non-Inputted Values
            if not request.args.get("volume"):
                volume = ""
            if not request.args.get("collateral"):
                collateral = ""
        else:
            m3 = 0
            extra = 0
            volume = ""
            volume_cost = 0
            collateral = ""
            collateral_cost = 0
            price = ""
    else:
        m3 = 0
        extra = 0
        volume = ""
        volume_cost = 0
        collateral = ""
        collateral_cost = 0
        price = ""

    # Warnings
    warning_list = []
    if (isinstance(price, float) or isinstance(price, int)) and price < 1000000:
        warning_list.append("Rewards must be at least 1M Isk")
        price = 1000000
    if (isinstance(volume, float) or isinstance(volume, int)) and volume > 300000:
        warning_list.append("Contracts must be less than 300k M3")
    if (isinstance(price, float) or isinstance(price, int)) and price > 1000000000:
        warning_list.append("Contracts should be below 1B isk")

    # Formatting
    extra = "{:0,.2f}".format(extra)
    volume_cost = "{:0,.2f}".format(volume_cost)
    collateral_cost = "{:0,.2f}".format(collateral_cost)
    price = "{:0,.2f}".format(price) if price else ""
    volume = "{:0.2f}".format(volume) if volume else ""
    collateral = "{:0.2f}".format(collateral) if collateral else ""

    # Contract History

    # Check Caches
    caches.stations()
    caches.contracts()

    users_set = set()
    # Find all users
    for contract in g.mongo.db.contracts.find({"service": "jf_service"}):
        users_set.update([contract["issuer_id"], contract["acceptor_id"]])
    caches.character(users_set)

    contract_list = [["Issuer", "Acceptor", "Start", "End", "Status", "Date Issued", "Expiration Date", "Volume"]]
    personal_contract_list = [["Acceptor", "Start", "End", "Status", "Date Issued", "Expiration Date", "Volume",
                               "Reward", "Collateral"]]

    next_update_query = g.mongo.db.contracts.find_one({"_id": "jf_service"})
    next_update = next_update_query["str_cache"] if next_update_query else "Unknown"
    for contract in g.mongo.db.contracts.find({"service": "jf_service"}):
        if contract["status"] not in ["Deleted", "Canceled"]:
            # Perform ID Conversions
            start_station = conversions.station(contract["start_station_id"])
            end_station = conversions.station(contract["end_station_id"])
            acceptor = conversions.character(contract["acceptor_id"])
            issuer = conversions.character(contract["issuer_id"])

            color = validator(start_station, end_station, contract)

            contract_list.append([
                color,
                issuer,
                acceptor,
                start_station,
                end_station,
                contract["status"],
                contract["date_issued"],
                contract["date_expired"],
                "{:0,.2f}".format(contract["volume"])
            ])

            if issuer == session["CharacterName"]:
                personal_contract_list.append([
                    color,
                    acceptor,
                    start_station,
                    end_station,
                    contract["status"],
                    contract["date_issued"],
                    contract["date_expired"],
                    "{:0,.2f}".format(contract["volume"]),
                    "{:0,.2f}".format(contract["reward"]),
                    "{:0,.2f}".format(contract["collateral"])
                ])

    # Check auth
    jf_admin = auth_check("jf_admin")
    jf_pilot = auth_check("jf_pilot")

    return render_template("jf.html", start_list=start_list, end_list=end_list, m3=m3, extra=extra, price=price,
                           volume=volume, contract_list=contract_list, next_update=next_update, admin=jf_admin,
                           collateral=collateral, volume_cost=volume_cost, collateral_cost=collateral_cost,
                           warning_list=warning_list, personal_contract_list=personal_contract_list, pilot=jf_pilot)


@jf.route('/admin', methods=["GET", "POST"])
@requires_sso('jf_admin')
def admin():
    route_list = []  # route = [_id, name, m3, extra]
    m3 = ""
    extra = ""
    _id = ""
    name = ""
    start = ""
    end = ""
    edit = False

    if request.method == "GET":
        if request.args.get("action") == "delete":
            g.mongo.db.jfroutes.remove({"_id": ObjectId(request.args.get("_id"))})
        elif request.args.get("action") == "edit":
            selected_route = g.mongo.db.jfroutes.find_one({"_id": ObjectId(request.args.get("_id"))})
            edit = True
            _id = request.args.get("_id")
            name = selected_route["name"]
            m3 = "{:0,.2f}".format(selected_route["m3"])
            extra = "{:0,.2f}".format(selected_route["extra"])
            start = selected_route["start"]
            end = selected_route["end"]
    elif request.method == "POST":
        m3 = request.form.get("m3") if request.form.get("m3") else 0
        extra = request.form.get("extra") if request.form.get("extra") else 0
        if request.form.get("action") == "single":
            if request.form.get("_id"):
                g.mongo.db.jfroutes.update({"_id": ObjectId(request.form.get("_id"))},
                                           {
                                               "name": request.form.get("name"),
                                               "m3": float(m3),
                                               "extra": float(extra),
                                               "start": request.form.get("start").strip(),
                                               "end": request.form.get("end").strip()
                                           }, upsert=True)
            else:
                g.mongo.db.jfroutes.insert({
                                               "name": request.form.get("name"),
                                               "m3": float(m3),
                                               "extra": float(extra),
                                               "start": request.form.get("start"),
                                               "end": request.form.get("end")
                                           })
        elif request.form.get("action") == "multiple":
            documents = []
            station_list = [x.strip() for x in request.form.get("stations").split("\n")]
            for start_station in station_list:
                for end_station in station_list:
                    if start_station != end_station:
                        documents.append({
                            "name": start_station.split(" - ")[0] + " >> " + end_station.split(" - ")[0],
                            "m3": 0,
                            "extra": 0,
                            "start": start_station,
                            "end": end_station
                        })
            g.mongo.db.jfroutes.insert(documents)

        # Clear all after post
        m3 = ""
        extra = ""
        _id = ""
        name = ""
        start = ""
        end = ""
        edit = False

    for route in g.mongo.db.jfroutes.find():
        route_list.append([route["_id"], route["name"],
                           "{:0,.2f}".format(route["m3"]), "{:0,.2f}".format(route["extra"]),
                           route["start"], route["end"]])

    return render_template("jf_admin.html", route_list=route_list, m3=m3, extra=extra, _id=_id, name=name,
                           start=start, end=end, edit=edit)


@jf.route('/pilot', methods=["GET", "POST"])
@requires_sso("jf_pilot")
def pilot():

    # Reservation System
    if request.method == "POST":
        if request.form.get("add"):
            add_ids = [int(x) for x in request.form.get("add").split(",")]
            for db_id in add_ids:
                g.mongo.db.contracts.update({"_id": db_id},
                                            {"$set": {"reserved_by": session["CharacterName"]}})
        elif request.form.get("remove"):
            remove_ids = [int(x) for x in request.form.get("remove").split(",")]
            for db_id in remove_ids:
                g.mongo.db.contracts.update({"_id": db_id},
                                            {"$unset": {"reserved_by": session["CharacterName"]}})

    caches.contracts()
    jf_contracts = g.mongo.db.contracts.find({
        "service": "jf_service",
        "status": "Outstanding"
    })
    contract_list = sorted(jf_contracts, key=itemgetter("volume"), reverse=True)

    total_volume = 0
    optimized_run = [["Issuer", "Start Station", "End Station", "Date Issued", "Date Expired", "Days", "Reward",
                      "Collateral", "Volume"]]
    reserved_contracts = [["Issuer", "Start Station", "End Station", "Date Issued", "Date Expired", "Days", "Reward",
                           "Collateral", "Volume"]]
    all_history = [["Issuer", "Start Station", "End Station", "Date Issued", "Date Expired", "Days", "Reward",
                    "Collateral", "Volume", "Reserved By"]]

    users_set = set()
    for contract in contract_list:
        users_set.update([contract["issuer_id"]])
    caches.character(users_set)
    optimized_reward = 0
    optimized_collateral = 0
    optimized_volume = 0
    optimized_ids = []

    for contract in contract_list:
        # Check for non-static stations
        start_station = conversions.station(contract["start_station_id"])
        end_station = conversions.station(contract["end_station_id"])
        issuer = conversions.character(contract["issuer_id"])
        color = validator(start_station, end_station, contract)

        if not contract.get("reserved_by") and total_volume + contract["volume"] <= 300000 and color not in ["active",
                                                                                                             "info"]:
            optimized_reward += contract["reward"]
            optimized_collateral += contract["collateral"]
            optimized_volume += contract["volume"]
            optimized_run.append([
                color,
                issuer,
                start_station,
                end_station,
                contract["date_issued"],
                contract["date_expired"],
                contract["num_days"],
                "{:0,.2f}".format(contract["reward"]),
                "{:0,.2f}".format(contract["collateral"]),
                "{:0,.2f}".format(contract["volume"])
            ])
            total_volume += contract["volume"]
            optimized_ids.append(str(contract["_id"]))
        if contract.get("reserved_by") == session["CharacterName"]:
            reserved_contracts.append([
                color,
                issuer,
                start_station,
                end_station,
                contract["date_issued"],
                contract["date_expired"],
                contract["num_days"],
                "{:0,.2f}".format(contract["reward"]),
                "{:0,.2f}".format(contract["collateral"]),
                "{:0,.2f}".format(contract["volume"])
            ])
        all_history.append([
            color,
            contract["_id"],
            issuer,
            start_station,
            end_station,
            contract["date_issued"],
            contract["date_expired"],
            contract["num_days"],
            "{:0,.2f}".format(contract["reward"]),
            "{:0,.2f}".format(contract["collateral"]),
            "{:0,.2f}".format(contract["volume"]),
            contract.get("reserved_by")
        ])

    # Formatting
    optimized_volume = "{:0,.2f}".format(optimized_volume)
    optimized_collateral = "{:0,.2f}".format(optimized_collateral)
    optimized_reward = "{:0,.2f}".format(optimized_reward)

    optimized_return = ",".join(optimized_ids)

    return render_template("jf_pilot.html", contract_list=contract_list, optimized_run=optimized_run,
                           reserved_contracts=reserved_contracts, all_history=all_history,
                           optimized_reward=optimized_reward, optimized_collateral=optimized_collateral,
                           optimized_volume=optimized_volume, optimized_return=optimized_return)
