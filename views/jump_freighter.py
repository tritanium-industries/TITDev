import os
import json
from operator import itemgetter
import time
import calendar

from flask import Blueprint, render_template, g, request, session, redirect, url_for

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


def validator(contract):
    with open("configs/base.json") as base_config_file:
        base_config = json.load(base_config_file)

    # Check if contract is valid
    validation_query = g.mongo.db.jf_routes.find_one({
        "_id": int(str(contract["start_station_id"]) + str(contract["end_station_id"]))})
    validation_failed = ["Failed", "Cancelled", "Rejected", "Reversed", "Deleted"]
    validation_completed = ["Completed", "CompletedByIssuer", "CompletedByContractor"]
    color = ""  # Default color, used for outstanding

    if validation_query:
        xml_time_pattern = "%Y-%m-%d %H:%M:%S"
        # Init with values if there will only be one price available
        corp_price = validation_query["prices"][0]["corp"]
        gen_price = validation_query["prices"][0]["general"]
        collateral_rate = validation_query["prices"][0]["collateral"]
        last_time = validation_query["prices"][0]["valid_after"]
        for validation_price in validation_query["prices"]:
            issue_time = int(calendar.timegm(time.strptime(contract["date_issued"], xml_time_pattern)))
            if issue_time >= validation_price["valid_after"] >= last_time:
                corp_price = validation_price["corp"]
                gen_price = validation_price["general"]
                collateral_rate = validation_price["collateral"]

        corp_check = g.mongo.db.characters.find_one({"_id": contract["issuer_id"],
                                                     "corporation_id": int(base_config["corporation_id"])})
        if corp_check:
            validation_calc = max(corp_price * contract["volume"] + contract["collateral"] * collateral_rate, 1000000)
        else:
            validation_calc = max(gen_price * contract["volume"] + contract["collateral"] * collateral_rate, 1000000)
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
def home():
    corp_rate = 0
    collateral_rate = 0
    general_rate = 0
    volume = ""
    corp_volume_cost = 0
    volume_cost = 0
    collateral = ""
    collateral_cost = 0
    price = ""
    corp_price = ""

    start_list = []
    end_list = []
    for station in g.mongo.db.jf_routes.distinct("start"):
        if request.args.get("start") == station:
            start_list.append([station, True])
        elif not request.args.get("start") and station == "Jita IV - Moon 4 - Caldari Navy Assembly Plant":
            start_list.append([station, True])
        else:
            start_list.append([station, False])
    for station in g.mongo.db.jf_routes.distinct("end"):
        if request.args.get("end") == station:
            end_list.append([station, True])
        elif not request.args.get("end") and station == "3KNA-N II - We have top men working on it":
            end_list.append([station, True])
        else:
            end_list.append([station, False])
    start_list.sort()
    end_list.sort()

    # Contract Calculations
    selected_route = "Not selected."
    input_error = False
    if request.args.get("start") and request.args.get("end"):
        start_station_id = g.mongo.db.stations.find_one({"name": request.args.get("start").strip()})["_id"]
        end_station_id = g.mongo.db.stations.find_one({"name": request.args.get("end").strip()})["_id"]
        selected_route = g.mongo.db.jf_routes.find_one({"_id": int(str(start_station_id) + str(end_station_id))})
        if selected_route:
            last_time = 0
            corp_rate = 0
            collateral_rate = 0
            general_rate = 0
            for price_history in selected_route["prices"]:
                if price_history["valid_after"] > last_time:
                    corp_rate = price_history["corp"]
                    general_rate = price_history["general"]
                    collateral_rate = price_history["collateral"]
                    last_time = price_history["valid_after"]

            try:
                volume = request.args.get("volume")
                volume = float(volume.replace(",", "") if volume else 0)
                collateral = request.args.get("collateral")
                collateral = float(collateral.replace(",", "") if collateral else 0)
            except ValueError:
                input_error = True
            else:
                volume_cost = general_rate * volume
                corp_volume_cost = corp_rate * volume
                collateral_cost = collateral * collateral_rate / 100.0
                price = volume_cost + collateral_cost
                corp_price = corp_volume_cost + collateral_cost

                # Mark Non-Inputted Values
                if not request.args.get("volume"):
                    volume = ""
                if not request.args.get("collateral"):
                    collateral = ""

    # Warnings
    warning_list = []
    if session.get("UI_Corporation"):
        compare_price = corp_price
    else:
        compare_price = price

    if input_error:
        warning_list.append("One of your inputs was not a number.")
    else:
        if compare_price and compare_price < 1000000:
            warning_list.append("Rewards must be at least 1M Isk")
            if session.get("UI_Corporation"):
                corp_price = 1000000
            else:
                price = 1000000
        if volume and volume > 300000:
            warning_list.append("Contracts must be less than 300k M3")
        if compare_price and compare_price > 1000000000:
            warning_list.append("Contracts should be below 1B isk")
        if not selected_route:
            warning_list.append("We do not service this route.")

        # Formatting
        corp_rate = "{:0,.2f}".format(corp_rate)
        volume_cost = "{:0,.2f}".format(volume_cost)
        corp_volume_cost = "{:0,.2f}".format(corp_volume_cost)
        collateral_cost = "{:0,.2f}".format(collateral_cost)
        collateral_rate = "{:0,.2f}".format(collateral_rate)
        price = "{:0,.2f}".format(price) if price else ""
        corp_price = "{:0,.2f}".format(corp_price) if price else ""
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

    next_update_query = g.mongo.db.caches.find_one({"_id": "jf_service"})
    next_update = next_update_query["cached_str"] if next_update_query else "Unknown"

    for contract in g.mongo.db.contracts.find({"service": "jf_service", "type": "Courier"}):
        if contract["status"] not in ["Deleted", "Canceled"]:
            # Perform ID Conversions
            start_station = g.mongo.db.stations.find_one({"_id": contract["start_station_id"]})
            start_station = start_station.get("name") if start_station else "Unknown"
            end_station = g.mongo.db.stations.find_one({"_id": contract["end_station_id"]})
            end_station = end_station.get("name") if end_station else "Unknown"
            acceptor = conversions.character(contract["acceptor_id"])
            issuer = conversions.character(contract["issuer_id"])

            color = validator(contract)

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

            if session.get("CharacterOwnerHash") and issuer == session["CharacterName"]:
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
    if session.get("CharacterOwnerHash"):
        jf_admin = auth_check("jf_admin")
        jf_pilot = auth_check("jf_pilot")
    else:
        jf_admin = None
        jf_pilot = None

    # Images
    with open("configs/base.json", "r") as base_config_file:
        base_config = json.load(base_config_file)
    corporation_logo = base_config["image_server"] + "Corporation/" + str(base_config["corporation_id"]) + "_128.png"
    alliance_logo = base_config["image_server"] + "Alliance/" + str(base_config["alliance_id"]) + "_128.png"

    return render_template("jf.html", start_list=start_list, end_list=end_list, general_rate=general_rate,
                           volume=volume, contract_list=contract_list, next_update=next_update, admin=jf_admin,
                           collateral=collateral, volume_cost=volume_cost, collateral_cost=collateral_cost,
                           warning_list=warning_list, personal_contract_list=personal_contract_list, pilot=jf_pilot,
                           corp_volume_cost=corp_volume_cost, corp_price=corp_price, corp_rate=corp_rate, price=price,
                           corporation_logo=corporation_logo, alliance_logo=alliance_logo,
                           collateral_rate=collateral_rate)


@jf.route('/admin', methods=["GET", "POST"])
@requires_sso('jf_admin')
def admin():
    route_list = []  # route = [_id, name, m3, corp]
    general = ""
    corp = ""
    _id = ""
    name = ""
    start = ""
    end = ""
    collateral = ""
    edit = False

    if request.method == "GET":
        if request.args.get("action") == "delete":
            g.mongo.db.jf_routes.remove({"_id": int(request.args.get("_id"))})
        elif request.args.get("action") == "edit":
            selected_route = g.mongo.db.jf_routes.find_one({"_id": int(request.args.get("_id"))})
            edit = True
            _id = request.args.get("_id")
            name = selected_route["name"]
            start = selected_route["start"]
            end = selected_route["end"]
            # Prices
            last_time = 0
            general = 0
            corp = 0
            collateral = 0
            for price in selected_route["prices"]:
                if price["valid_after"] > last_time:
                    corp = price["corp"]
                    general = price["general"]
                    last_time = price["valid_after"]
                    collateral = price["collateral"]
        elif request.args.get("action") == "all":
            bulk_op = g.mongo.db.jf_routes.initialize_unordered_bulk_op()
            bulk_run = False
            for route in g.mongo.db.jf_routes.find():
                all_last_time = 0
                all_corp = 0
                all_general = 0
                for price in route["prices"]:
                    if price["valid_after"] > all_last_time:
                        all_last_time = price["valid_after"]
                        all_corp = price["corp"]
                        all_general = price["general"]
                bulk_run = True
                bulk_op.find({"_id": route["_id"]}).update({
                    "$push": {
                        "prices": {
                            "valid_after": int(time.time()),
                            "corp": all_corp,
                            "general": all_general,
                            "collateral": float(request.args.get("collateral"))
                        }
                    }
                })

            if bulk_run:
                bulk_op.execute()

    elif request.method == "POST":
        if request.form.get("action") == "single":
            if request.form.get("_id"):
                g.mongo.db.jf_routes.update({"_id": int(request.form.get("_id"))},
                                            {
                                                "$set": {
                                                    "name": request.form.get("name"),
                                                    "start": request.form.get("start").strip(),
                                                    "end": request.form.get("end").strip()
                                                },
                                                "$push": {
                                                    "prices": {
                                                        "valid_after": int(time.time()),
                                                        "corp": float(request.form.get("corp", 0)),
                                                        "general": float(request.form.get("general", 0)),
                                                        "collateral": float(request.form.get("collateral", 0))
                                                    }
                                                }
                                            }, upsert=True)
            else:
                db_start = g.mongo.db.stations.find_one({"name": request.form.get("start").strip()})
                db_end = g.mongo.db.stations.find_one({"name": request.form.get("end").strip()})
                if db_start and db_end:
                    g.mongo.db.jf_routes.insert({
                        "$set": {
                            "_id": int(str(db_start["_id"] + str(db_end["_id"]))),
                            "name": request.form.get("name"),
                            "start": request.form.get("start").strip(),
                            "end": request.form.get("end").strip()
                        },
                        "$push": {
                            "prices": {
                                "valid_after": int(time.time()),
                                "corp": float(request.form.get("corp", 0)),
                                "general": float(request.form.get("general", 0)),
                                "collateral": float(request.form.get("collateral", 0))
                            }
                        }
                    })
        elif request.form.get("action") == "multiple":
            station_list = [g.mongo.db.stations.find_one({"name": x.strip()})
                            for x in request.form.get("stations").split("\n")]
            if station_list:  # Ensure there are stations to parse
                bulk_op = g.mongo.db.jf_routes.initialize_unordered_bulk_op()
                for start_station in station_list:
                    for end_station in station_list:
                        if start_station != end_station:
                            route_id = int(str(start_station["_id"]) + str(end_station["_id"]))
                            route_name_start = start_station["name"].split(" - ")[0]
                            route_name_end = end_station["name"].split(" - ")[0]
                            bulk_op.find({"_id": route_id}).upsert().update({
                                "$setOnInsert": {
                                    "name": route_name_start + " >> " + route_name_end,
                                    "start": start_station["name"].strip(),
                                    "end": end_station["name"].strip(),
                                    "prices": [{
                                        "valid_after": int(time.time()),
                                        "corp": float(request.form.get("corp", 0)),
                                        "general": float(request.form.get("general", 0)),
                                        "collateral": float(request.form.get("collateral", 0))
                                    }]
                                }
                            })
                bulk_op.execute()

        # Clear all after post
        general = ""
        corp = ""
        _id = ""
        name = ""
        start = ""
        end = ""
        collateral = ""
        edit = False

    for route in g.mongo.db.jf_routes.find():
        last_time = 0
        corp_price = 0
        gen_price = 0
        collateral_percent = 0
        for price in route["prices"]:
            if price["valid_after"] > last_time:
                corp_price = price["corp"]
                gen_price = price["general"]
                last_time = price["valid_after"]
                collateral_percent = price["collateral"]

        route_list.append([route["_id"], route["name"],
                           "{:0,.2f}".format(gen_price), "{:0,.2f}".format(corp_price),
                           "{:0,.2f}".format(collateral_percent),
                           route["start"], route["end"]])

    return render_template("jf_admin.html", route_list=route_list, general=general, corp=corp, _id=_id, name=name,
                           start=start, end=end, edit=edit, collateral=collateral)


@jf.route('/pilot', methods=["GET", "POST"])
@requires_sso("jf_pilot")
def pilot():
    # Reservation System
    if request.method == "POST":
        bulk_op = g.mongo.db.contracts.initialize_unordered_bulk_op()
        bulk_run = False
        if request.form.get("add"):
            bulk_run = True
            add_ids = [int(x) for x in request.form.get("add").split(",")]
            for db_id in add_ids:
                bulk_op.find({"_id": db_id}).update({"$set": {"reserved_by": session["CharacterName"]}})
        elif request.form.get("remove"):
            bulk_run = True
            remove_ids = [int(x) for x in request.form.get("remove").split(",")]
            for db_id in remove_ids:
                bulk_op.find({"_id": db_id}).update({"$unset": {"reserved_by": session["CharacterName"]}})
        if bulk_run:
            bulk_op.execute()

    # JF Corporation Contracts
    caches.contracts()
    jf_contracts = g.mongo.db.contracts.find({
        "service": "jf_service",
        "status": "Outstanding",
        "type": "Courier"
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
    reserved_reward = 0
    reserved_collateral = 0
    reserved_volume = 0
    reserved_ids = []

    for contract in contract_list:
        # Check for non-static stations
        start_station = g.mongo.db.stations.find_one({"_id": contract["start_station_id"]})["name"]
        end_station = g.mongo.db.stations.find_one({"_id": contract["end_station_id"]})["name"]
        issuer = conversions.character(contract["issuer_id"])
        color = validator(contract)

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
            reserved_reward += contract["reward"]
            reserved_collateral += contract["collateral"]
            reserved_volume += contract["volume"]
            reserved_contracts.append([
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
                "{:0,.2f}".format(contract["volume"])
            ])
            reserved_ids.append(str(contract["_id"]))
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
    reserved_volume = "{:0,.2f}".format(reserved_volume)
    reserved_collateral = "{:0,.2f}".format(reserved_collateral)
    reserved_reward = "{:0,.2f}".format(reserved_reward)

    optimized_return = ",".join(optimized_ids)
    reserved_return = ",".join(reserved_ids)

    # Personal Contracts
    personal_api_keys = g.mongo.db.api_keys.find_one({"_id": session["CharacterOwnerHash"]})
    if personal_api_keys:
        invalid_apis = caches.contracts([("personal", api_key["key_id"], api_key["vcode"],
                                          api_key["character_id"]) for api_key in personal_api_keys["keys"]])
    if invalid_apis:
        return redirect(url_for("account.home", keys=",".join([str(x) for x in invalid_apis])))
    personal_character_ids = [x["character_id"] for x in personal_api_keys["keys"]]
    personal_contracts = g.mongo.db.contracts.find({
        "service": "personal",
        "status": "Outstanding",
        "type": "Courier",
        "assignee_id": {"$in": personal_character_ids}
    })
    users_set = set()
    for contract in personal_contracts:
        users_set.update([contract["issuer_id"], contract["assignee_id"]])
    caches.character(users_set)

    # Call db again because query has ended when updating cache
    personal_contracts = g.mongo.db.contracts.find({
        "service": "personal",
        "status": "Outstanding",
        "type": "Courier",
        "assignee_id": {"$in": personal_character_ids}
    })
    personal_history = [["Assignee", "Issuer", "Start Station", "End Station", "Date Issued", "Date Expired", "Days",
                         "Reward", "Collateral", "Volume"]]
    for contract in personal_contracts:
        start_station = g.mongo.db.stations.find_one({"_id": contract["start_station_id"]})["name"]
        end_station = g.mongo.db.stations.find_one({"_id": contract["end_station_id"]})["name"]
        issuer = conversions.character(contract["issuer_id"])
        assignee = conversions.character(contract["assignee_id"])
        color = validator(contract)

        personal_history.append([
            color,
            assignee,
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

    return render_template("jf_pilot.html", contract_list=contract_list, optimized_run=optimized_run,
                           reserved_contracts=reserved_contracts, all_history=all_history,
                           optimized_reward=optimized_reward, optimized_collateral=optimized_collateral,
                           optimized_volume=optimized_volume, optimized_return=optimized_return,
                           reserved_reward=reserved_reward, reserved_collateral=reserved_collateral,
                           reserved_volume=reserved_volume, reserved_return=reserved_return,
                           personal_history=personal_history)
