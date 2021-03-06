import json
import time
import re
import datetime

from flask import Blueprint, render_template, session, g, request, redirect, url_for, flash, get_flashed_messages

from bson.objectid import ObjectId
import bson.errors

from views.auth import requires_sso, auth_check
from helpers.eve_central import market_hub_prices
from helpers import conversions

ordering = Blueprint("ordering", __name__, template_folder="templates")


@ordering.route("/", methods=["GET", "POST"])
@ordering.route("/<item>", methods=["GET", "POST"])
@requires_sso("alliance")
def home(item=""):
    cart_item_list = {}
    error_string = request.args.get("error_string")

    bulk_op_update = g.mongo.db.carts.initialize_unordered_bulk_op()
    bulk_run_update = False

    # Change qty if post from this page
    if request.method == "POST" and request.form.get("action") == "qty":
        for key, value in request.form.items():
            if key != "action" and not key.startswith("DataTables"):
                if value.strip():
                    if int(float(value)) != 0:
                        bulk_op_update.find({"_id": session["CharacterOwnerHash"]}).upsert().update({
                            "$set": {"items." + key: int(float(value))}})
                    else:
                        bulk_op_update.find({"_id": session["CharacterOwnerHash"]}).upsert().update({
                            "$unset": {"items." + key: int(float(value))}})
                    bulk_run_update = True
    if request.method == "POST" and request.form.get("action") == "clear":
        g.mongo.db.carts.remove({"_id": session["CharacterOwnerHash"]})

    # Add new item to database
    input_string = request.form.get("parse", session.get("fitting"))
    if item or input_string:
        if item:
            try:
                item_adjustment_list = item.split(":")
                for adjustment_item in item_adjustment_list:
                    adjustment_item_info = adjustment_item.split(";")
                    if request.args.get("action") == "edit":
                        bulk_op_update.find({"_id": session["CharacterOwnerHash"]}).upsert().update({
                            "$set": {"items." + adjustment_item_info[0]: int(adjustment_item_info[1])}})
                    else:
                        bulk_op_update.find({"_id": session["CharacterOwnerHash"]}).upsert().update({
                            "$inc": {"items." + adjustment_item_info[0]: int(adjustment_item_info[1])}})
                    bulk_run_update = True
            except IndexError:
                error_string = "Was not able to add {}".format(item)
        elif input_string:
            session.pop("fitting", None)
            parse_error = None
            parse_result = ""
            try:
                if input_string.startswith("["):
                    eft_parser = conversions.eft_parsing(input_string)
                    parse_result = eft_parser[3]  # DNA String
                    parse_error = eft_parser[4]
                else:
                    parse_array = []
                    item_input, item_qty = conversions.manual_parsing(input_string)[1:3]
                    pre_parse_db = g.mongo.db.items.find({"name": {"$in": item_input}})
                    for pre_parse_item in pre_parse_db:
                        parse_array.append(str(pre_parse_item["_id"]) + ";" +
                                           str(item_qty[pre_parse_item["name"].upper()]))
                    if len(parse_array) != len(item_input):
                        error_string = "There is an item that could not be parsed. Check your input and try again."
                    parse_result = ":".join(parse_array)
            except KeyError:
                error_string = "Could not parse the input. Please ensure it is correctly formatted."
            if parse_error:
                if parse_error == "parsing":
                    error_string = "Could not parse the EFT-Formatted fit. Please ensure it is correctly formatted."
                else:
                    error_string = parse_error
            parse_item_list = parse_result.split(":")
            for parse_item in parse_item_list:
                if parse_item:
                    direct_info = parse_item.split(";")
                    if len(direct_info) == 1:
                        bulk_op_update.find({"_id": session["CharacterOwnerHash"]}).upsert().update({
                            "$inc": {"items." + direct_info[0]: 1}})
                        bulk_run_update = True
                    else:
                        bulk_op_update.find({"_id": session["CharacterOwnerHash"]}).upsert().update({
                            "$inc": {"items." + direct_info[0]: int(direct_info[1])}})
                        bulk_run_update = True

    if bulk_run_update:
        bulk_op_update.execute()

    if item or input_string:
        flash(error_string)
        return redirect(url_for("ordering.home"))
    else:
        error_string = get_flashed_messages()
        error_string = error_string[0] if error_string else None

    # Load cart
    total_volume = 0
    sell_price = 0
    current_cart = g.mongo.db.carts.find_one({"_id": session["CharacterOwnerHash"]})
    fittings_info = []
    fittings_breakdown = {}

    # Redirect to fittings if saving pack
    ordering_admin = auth_check("ordering_admin")
    if ordering_admin and request.form.get("action") == "pack":
        pack_fit = {
            "fit": "",
            "items": {},
            "submitter": session["CharacterOwnerHash"],
            "price": 0,
            "volume": 0,
            "name": request.form.get("pack"),
            "notes": None,
            "dna": None,
            "ship": "Pack",
            "source": None,
            "doctrine": False
        }
        fit_array = []
        dna_array = []
        for table_key, table_info in current_cart.get("item_table", {}).items():
            pack_fit["items"][table_info["name"]] = table_info["qty"]
            fit_array.append(table_info["name"] + " " + str(table_info["qty"]))
            dna_array.append(table_key + ";" + str(table_info["qty"]))
        pack_fit["fit"] = "\n".join(fit_array)
        pack_fit["dna"] = ":".join(dna_array)

        fit_id = g.mongo.db.fittings.insert(pack_fit)
        return redirect(url_for("fittings.fit", fit_id=fit_id))

    # Determine processing cost
    order_db = g.mongo.db.preferences.find_one({"_id": "ordering"})
    if session["UI_Corporation"]:
        order_tax = order_db.get("tax_corp", 0) if order_db else 0
    else:
        order_tax = order_db.get("tax", 0) if order_db else 0

    # Continue loading cart
    if current_cart and current_cart.get("items"):
        cart_item_list_pre = current_cart["items"]

        # Filter fittings from items
        fittings_id_list = []
        for cart_item_id, cart_item_qty in cart_item_list_pre.items():
            try:
                fittings_id_list.append(ObjectId(cart_item_id))
            except bson.errors.InvalidId:
                cart_item_list[cart_item_id] = cart_item_qty

        # Unpack fittings
        for selected_fit in g.mongo.db.fittings.find({"_id": {"$in": fittings_id_list}}):
            fit_item_list = selected_fit["dna"].split(":")
            fittings_info.append([str(selected_fit["_id"]),
                                  "[Fit] " + selected_fit["name"],
                                  current_cart["items"][str(selected_fit["_id"])],
                                  "{:,.02f}".format(selected_fit["volume"]),
                                  "{:,.02f}".format(selected_fit["price"] * (1 + order_tax / 100)),
                                  "{:,.02f}".format(selected_fit["volume"] *
                                                    current_cart["items"][str(selected_fit["_id"])]),
                                  "{:,.02f}".format(selected_fit["price"] *
                                                    current_cart["items"][str(selected_fit["_id"])] *
                                                    (1 + order_tax / 100))
                                  ])
            sell_price += selected_fit["price"] * current_cart["items"][str(selected_fit["_id"])]
            for fit_item in fit_item_list:
                if fit_item:
                    item_info = fit_item.split(";")
                    fittings_breakdown.setdefault(item_info[0], 0)
                    if len(item_info) == 1:
                        fittings_breakdown[item_info[0]] += 1 * current_cart["items"][str(selected_fit["_id"])]
                    else:
                        fittings_breakdown[item_info[0]] += int(item_info[1]) * current_cart["items"][
                            str(selected_fit["_id"])]

    cart_item_list_int = [int(x) for x in cart_item_list.keys()]
    fittings_breakdown_int = [int(x) for x in fittings_breakdown.keys()]
    prices_int = cart_item_list_int + fittings_breakdown_int
    prices, prices_usable = market_hub_prices(prices_int) if prices_int else ({}, True)

    full_cart = {}

    invoice_info = [["Name", "Qty", "Vol/Item", "Isk/Item + Markup",
                     "Vol Subtotal", "Isk Subtotal w/ Markup"]] + fittings_info
    for db_item in g.mongo.db.items.find({"_id": {"$in": cart_item_list_int}}):
        invoice_info.append([
            db_item["_id"],
            db_item["name"],
            cart_item_list[str(db_item["_id"])],
            "{:,.02f}".format(db_item["volume"]),
            "{:,.02f}".format(prices[db_item["_id"]]["sell"] * (1 + order_tax / 100)),
            "{:,.02f}".format(db_item["volume"] * cart_item_list[str(db_item["_id"])]),
            "{:,.02f}".format(prices[db_item["_id"]]["sell"] * cart_item_list[str(db_item["_id"])] *
                              (1 + order_tax / 100))
        ])
        full_cart[str(db_item["_id"])] = {
            "name": db_item["name"],
            "qty": cart_item_list[str(db_item["_id"])],
            "volume": db_item["volume"],
            "price": prices[db_item["_id"]]["sell"],
            "volume_total": db_item["volume"] * cart_item_list[str(db_item["_id"])],
            "price_total": prices[db_item["_id"]]["sell"] * cart_item_list[str(db_item["_id"])]
        }
        total_volume += db_item["volume"] * cart_item_list[str(db_item["_id"])]
        sell_price += prices[db_item["_id"]]["sell"] * cart_item_list[str(db_item["_id"])]

    breakdown_info = [["Name", "Qty", "Vol/Item", "Isk/Item + Markup", "Vol Subtotal", "Isk Subtotal w/ Markup"]]
    for db_item_breakdown in g.mongo.db.items.find({"_id": {"$in": fittings_breakdown_int}}):
        breakdown_info.append([
            db_item_breakdown["_id"],
            db_item_breakdown["name"],
            fittings_breakdown[str(db_item_breakdown["_id"])],
            "{:,.02f}".format(db_item_breakdown["volume"]),
            "{:,.02f}".format(prices[int(db_item_breakdown["_id"])]["sell"] * (1 + order_tax / 100)),
            "{:,.02f}".format(db_item_breakdown["volume"] * fittings_breakdown[str(db_item_breakdown["_id"])]),
            "{:,.02f}".format(prices[int(db_item_breakdown["_id"])]["sell"] *
                              fittings_breakdown[str(db_item_breakdown["_id"])] * (1 + order_tax / 100))
        ])
        total_volume += db_item_breakdown["volume"] * fittings_breakdown[str(db_item_breakdown["_id"])]
        if full_cart.get(str(db_item_breakdown["_id"])):
            full_cart[str(db_item_breakdown["_id"])]["qty"] += fittings_breakdown[str(db_item_breakdown["_id"])]
            full_cart[str(db_item_breakdown["_id"])]["volume_total"] += (db_item_breakdown["volume"] *
                                                                         fittings_breakdown[
                                                                             str(db_item_breakdown["_id"])])
            full_cart[str(db_item_breakdown["_id"])]["price_total"] += (prices[int(db_item_breakdown["_id"])]["sell"] *
                                                                        fittings_breakdown[
                                                                            str(db_item_breakdown["_id"])])
        else:
            full_cart[str(db_item_breakdown["_id"])] = {
                "id": db_item_breakdown["_id"],
                "name": db_item_breakdown["name"],
                "qty": fittings_breakdown[str(db_item_breakdown["_id"])],
                "volume": db_item_breakdown["volume"],
                "price": prices[int(db_item_breakdown["_id"])]["sell"],
                "volume_total": db_item_breakdown["volume"] * fittings_breakdown[str(db_item_breakdown["_id"])],
                "price_total": (prices[int(db_item_breakdown["_id"])]["sell"] *
                                fittings_breakdown[str(db_item_breakdown["_id"])])
            }

    # List routes
    with open("configs/base.json") as base_config_file:
        base_config = json.load(base_config_file)

    market_hub_name = base_config["market_hub_name"]
    min_id_limit = base_config["market_hub_station"] * 100000000
    max_id_limit = base_config["market_hub_station"] * 100000000 + 100000000
    market_hub_routes = g.mongo.db.jf_routes.find({"_id": {"$gte": min_id_limit, "$lt": max_id_limit}})

    if request.args.get("end"):
        selected_route = int(request.args.get("end"))
    else:
        selected_route = 0

    valid_stations = []
    route_name = ""
    new_cart = g.mongo.db.carts.find_one({"_id": session["CharacterOwnerHash"]})
    for route in market_hub_routes:
        if request.args.get("end"):
            if route["_id"] == int(request.args.get("end")):
                valid_stations.append([route["_id"], route["end"], True])
                selected_route = route["_id"] if selected_route == 0 else selected_route
                route_name = route["end"]
            else:
                valid_stations.append([route["_id"], route["end"], False])
        elif new_cart:
            if route["_id"] == new_cart.get("route"):
                valid_stations.append([route["_id"], route["end"], True])
                selected_route = route["_id"] if selected_route == 0 else selected_route
                route_name = route["end"]
            else:
                valid_stations.append([route["_id"], route["end"], False])
        elif not request.args.get("end") and route["end"] == base_config["default_ship_to"]:
            valid_stations.append([route["_id"], route["end"], True])
            selected_route = route["_id"] if selected_route == 0 else selected_route
            route_name = route["end"]
        else:
            valid_stations.append([route["_id"], route["end"], False])

    # JF Calculations
    if selected_route == 0:
        selected_route = int(str(base_config["market_hub_station"]) + str(base_config["home_station"]))
    selected_route_info = g.mongo.db.jf_routes.find_one({"_id": selected_route})
    if selected_route_info:
        rate_info = conversions.valid_value(selected_route_info["prices"], time.time())
        if session.get("UI_Corporation"):
            jf_rate = rate_info["corp"]
        else:
            jf_rate = rate_info["general"]

        jf_total = jf_rate * total_volume
        # Min 1 Mil Isk
        if jf_total < 1000000:
            jf_total = 1000000
    else:
        jf_rate = 0
        jf_total = 0

    order_tax_total = sell_price * order_tax / 100
    order_total = jf_total + sell_price + order_tax_total

    # List of characters and notes
    character_list = []
    db_api_list = g.mongo.db.api_keys.find_one({"_id": session["CharacterOwnerHash"]})
    if not request.args.get("action") == "character" and current_cart and request.args.get("action") != "order":
        current_character = current_cart.get("contract_to")
    elif request.args.get("character"):
        current_character = request.args.get("character")
    else:
        current_character = session["CharacterName"]
    if not request.args.get("action") == "notes" and current_cart and request.args.get("action") != "order":
        notes = current_cart.get("notes", "")
    else:
        notes = request.args.get("notes", "")
    if db_api_list:
        for character in db_api_list["keys"]:
            if character["character_name"] == current_character:
                character_list.append((character["character_name"], True))
            else:
                character_list.append((character["character_name"], False))

    # Update DB
    g.mongo.db.carts.update({"_id": session["CharacterOwnerHash"]},
                            {"$set": {
                                "item_table": full_cart,
                                "route": selected_route,
                                "volume": total_volume,
                                "jf_rate": jf_rate,
                                "jf_total": jf_total,
                                "sell_price": sell_price,
                                "order_total": order_total,
                                "jf_end": route_name,
                                "order_tax": order_tax,
                                "order_tax_total": order_tax_total,
                                "prices_usable": prices_usable,
                                "notes": notes,
                                "contract_to": current_character
                            }}, upsert=True)

    if request.args.get("action") == "order":
        return redirect(url_for("ordering.invoice"))

    # Round order total
    order_total = round(order_total + 50000, -5)

    # Formatting
    total_volume = "{:,.02f}".format(total_volume)
    sell_price = "{:,.02f}".format(sell_price)
    jf_total = "{:,.02f}".format(jf_total)
    order_total = "{:,.02f}".format(order_total)
    order_tax = "{:,.02f}".format(order_tax)
    order_tax_total = "{:,.02f}".format(order_tax_total)

    return render_template("ordering.html", invoice_info=invoice_info, total_volume=total_volume,
                           sell_price=sell_price, valid_stations=valid_stations, jf_rate=jf_rate,
                           jf_total=jf_total, order_total=order_total, market_hub_name=market_hub_name,
                           prices_usable=prices_usable, error_string=error_string, breakdown_info=breakdown_info,
                           order_tax=order_tax, order_tax_total=order_tax_total, character_list=character_list,
                           notes=notes, ordering_admin=ordering_admin)


@ordering.route("/search")
@requires_sso("alliance")
def search():
    if request.args.get("id"):
        return redirect(url_for("ordering.home",
                                item=request.args.get("id") + ";" + request.args.get("qty-" +
                                                                                     request.args.get("id"), 1)))
    elif not request.args.get("name"):
        return redirect(url_for("ordering.home"))

    regex_search = re.compile(request.args.get("name", ""), re.IGNORECASE)
    item_list = {}
    for item in g.mongo.db.items.find({"name": regex_search}):
        if item["name"].find("SKIN") == -1 and item["name"].lower().find("blueprint") == -1:
            item_list[item["_id"]] = [item["name"], "{:,.02f}".format(item["volume"])]

    prices, prices_usable = market_hub_prices(list(item_list.keys()))
    search_table = []
    for key, value in item_list.items():
        search_table.append(value + ["{:,.02f}".format(prices[key]["sell"]), 1, key])

    with open("configs/base.json") as base_config_file:
        base_config = json.load(base_config_file)

    market_hub_name = base_config["market_hub_name"]

    return render_template("ordering_search.html", search_table=search_table, market_hub_name=market_hub_name,
                           prices_usable=prices_usable)


@ordering.route("/invoice/<invoice_id>", methods=["GET", "POST"])
@ordering.route("/invoice")
@requires_sso("alliance")
def invoice(invoice_id=""):
    timestamp = None
    current_time = None

    with open("configs/base.json") as base_config_file:
        base_config = json.load(base_config_file)
    market_hub_name = base_config["market_hub_name"]

    if not invoice_id:
        cart = g.mongo.db.carts.find_one({"_id": session["CharacterOwnerHash"]})
        if request.args.get("action") == "order":
            cart["user"] = cart.pop("_id")
            cart["external"] = False
            cart["character"] = session["CharacterName"]
            cart["status"] = "Submitted"
            invoice_id = g.mongo.db.invoices.insert(cart)
            g.mongo.db.carts.remove({"_id": cart["user"]})

            # Discord Integration
            g.redis.publish('titdev-marketeer',
                            "@everyone: {0} has created an invoice: {1}".format(
                                session["CharacterName"],
                                url_for("ordering.invoice", invoice_id=invoice_id, _external=True)
                            ))
            return redirect(url_for("ordering.invoice", invoice_id=invoice_id))
    else:
        cart = g.mongo.db.invoices.find_one({"_id": ObjectId(invoice_id)})
        timestamp = ObjectId(invoice_id).generation_time.strftime("%Y-%m-%d %H:%M:%S")
        if not cart:
            return redirect(url_for("account.home"))

    # Invoice Editing
    status = cart.get("status", "Not Processed")
    ordering_admin = auth_check("ordering_admin")
    ordering_marketeer = auth_check("ordering_marketeer")
    editor = True if ordering_admin or ordering_marketeer else False
    can_delete = True if status == "Submitted" and (
        cart.get("user") == session["CharacterOwnerHash"] or ordering_admin) else False
    can_edit = True if ordering_admin or (
        cart.get("marketeer") == session["CharacterName"] or status == "Submitted") else False
    if request.method == "POST":
        # Not Processed, Failed, Processing, Submitted, Hold, Rejected
        if request.form.get("action") == "delete" and can_delete:
            g.mongo.db.invoices.remove({"_id": ObjectId(invoice_id)})
            return redirect(url_for("account.home"))
        elif request.form.get("action") == "reject" and status in ["Not Processed", "Failed",
                                                                   "Processing", "Submitted", "Hold"] and editor:
            current_time = int(time.time())
            g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)}, {"$set": {"status": "Rejected",
                                                                                "marketeer": session["CharacterName"],
                                                                                "reason": request.form.get("reason"),
                                                                                "finish_time": current_time
                                                                                }})
            # Discord Integration
            g.redis.publish('titdev-marketeer',
                            "{0} has rejected an invoice: {1}".format(
                                session["CharacterName"],
                                url_for("ordering.invoice", invoice_id=invoice_id, _external=True)
                            ))
        elif request.form.get("action") == "process" and status in ["Not Processed", "Failed", "Rejected",
                                                                    "Submitted", "Hold"] and editor:
            g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)}, {"$set": {"status": "Processing",
                                                                                "marketeer": session["CharacterName"]
                                                                                },
                                                                       "$unset": {
                                                                           "reason": request.form.get("reason")}})
        elif request.form.get("action") == "release" and status in ["Processing", "Failed", "Rejected", "Hold"] and (
                        cart.get("marketeer") == session["CharacterName"] or ordering_admin
        ):
            g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)}, {"$unset": {"marketeer": session["CharacterName"],
                                                                                  "reason": request.form.get("reason")
                                                                                  },
                                                                       "$set": {"status": "Submitted"}})
        elif request.form.get("action") == "hold" and status in ["Failed", "Processing", "Submitted", "Rejected"]:
            if g.mongo.db.invoices.find_one({"_id": ObjectId(invoice_id)}).get("status") != "Hold":
                g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)},
                                           {
                                               "$set": {"status": "Hold",
                                                        "marketeer": session["CharacterName"],
                                                        "reason": request.form.get("reason")}
                                           })
                # Discord Integration
                g.redis.publish('titdev-marketeer',
                                "@everyone: {0} has put an invoice on hold: {1} for the following reason: {2}".format(
                                    session["CharacterName"],
                                    url_for("ordering.invoice", invoice_id=invoice_id, _external=True),
                                    request.form.get("reason")
                                ))
        elif request.form.get("action") == "fail" and editor:
            current_time = int(time.time())
            g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)}, {"$set": {"status": "Failed",
                                                                                "marketeer": session["CharacterName"],
                                                                                "reason": request.form.get("reason"),
                                                                                "finish_time": current_time
                                                                                }})
            # Discord Integration
            g.redis.publish('titdev-marketeer',
                            "{0} has failed an invoice: {1} for the following reason: {2}".format(
                                session["CharacterName"],
                                url_for("ordering.invoice", invoice_id=invoice_id, _external=True),
                                request.form.get("reason")
                            ))
        elif request.form.get("action") == "complete" and editor:
            if g.mongo.db.invoices.find_one({"_id": ObjectId(invoice_id)}).get("status") != "Completed":
                current_time = int(time.time())
                g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)},
                                           {
                                               "$set": {"status": "Completed",
                                                        "marketeer": session["CharacterName"],
                                                        "finish_time": current_time},
                                               "$unset": {"reason": request.form.get("reason")}
                                           })
                # Discord Integration
                g.redis.publish('titdev-marketeer',
                                "{0} has completed an invoice: {1}".format(
                                    session["CharacterName"],
                                    url_for("ordering.invoice", invoice_id=invoice_id, _external=True)
                                ))
        elif request.form.get("action") == "shipping" and editor:
            g.mongo.db.invoices.update({"_id": ObjectId(invoice_id)}, {"$set": {
                "external": not cart.get("external", False)}})
        return redirect(url_for("ordering.invoice", invoice_id=invoice_id))

    # Check shipping
    if not cart.get("external") and status == "Processing":
        shipping_contract = g.mongo.db.contracts.find_one({"title": invoice_id})
        if shipping_contract:
            status = "Shipping - " + shipping_contract["status"]

    # Set buttons
    if status == "Not Processed" or status == "Failed" or status == "Submitted":
        button = "Process"
    else:
        button = "Release"

    invoice_info = [["Name", "Qty", "Vol/Item", "Isk/Item", "Vol Subtotal", "Isk Subtotal"]]
    for item in cart["item_table"].values():
        invoice_info.append([item["name"], item["qty"], "{:,.02f}".format(item["volume"]),
                             "{:,.02f}".format(item["price"]), "{:,.02f}".format(item["volume_total"]),
                             "{:,.02f}".format(item["price_total"])])

    # Round order total
    cart["order_total"] = round(cart["order_total"] + 50000, -5)

    # Formatting
    finish_time = cart.get("finish_time", current_time)
    if finish_time:
        finish_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(finish_time))

    return render_template("ordering_invoice.html", invoice_info=invoice_info, market_hub_name=market_hub_name,
                           prices_usable=cart["prices_usable"], total_volume="{:,.02f}".format(cart["volume"]),
                           sell_price="{:,.02f}".format(cart["sell_price"]),
                           order_tax_total="{:,.02f}".format(cart["order_tax_total"]),
                           order_tax="{:,.02f}".format(cart["order_tax"]), jf_end=cart["jf_end"],
                           jf_rate="{:,.02f}".format(cart["jf_rate"]),
                           jf_total="{:,.02f}".format(cart["jf_total"]),
                           order_total="{:,.02f}".format(cart["order_total"]),
                           timestamp=timestamp, can_delete=can_delete, editor=editor,
                           status=status, button=button, marketeer=cart.get("marketeer"), reason=cart.get("reason"),
                           external=cart.get("external", False), can_edit=can_edit, character=cart.get("character"),
                           contract_to=cart.get("contract_to"), notes=cart.get("notes"), invoice_id=invoice_id,
                           finish_time=finish_time)


@ordering.route("/admin", methods=["GET", "POST"])
@requires_sso("ordering_marketeer", "ordering_admin")
def admin():
    # Auth Check
    is_admin = auth_check("ordering_admin")
    if request.form.get("action") == "tax" and request.form.get("tax") and is_admin:
        g.mongo.db.preferences.update({"_id": "ordering"}, {"$set": {"tax": float(request.form.get("tax", 0))}},
                                      upsert=True)
    elif request.form.get("action") == "tax_corp" and request.form.get("tax") and is_admin:
        g.mongo.db.preferences.update({"_id": "ordering"}, {"$set": {"tax_corp": float(request.form.get("tax", 0))}},
                                      upsert=True)
    tax_db = g.mongo.db.preferences.find_one({"_id": "ordering"})
    tax = "{:.02f}".format(tax_db.get("tax", 0)) if tax_db else 0
    tax_corp = "{:.02f}".format(tax_db.get("tax_corp", 0)) if tax_db else 0

    # Invoice List
    one_month_oid = ObjectId.from_datetime(datetime.datetime.today() - datetime.timedelta(30))
    invoice_table = []
    marketeer_invoice_table = []
    new_invoice_table = []
    for invoice_db in g.mongo.db.invoices.find({"$or": [{"_id": {"$gt": one_month_oid}},
                                                        {"status": {"$not": re.compile("Completed")}}]}):
        invoice_status = invoice_db.get("status", "Not Processed")
        invoice_timestamp = ObjectId(invoice_db["_id"]).generation_time.strftime("%Y-%m-%d %H:%M:%S")
        invoice_color = ""
        if invoice_status == "Shipping - Completed":
            invoice_color = "primary"
        elif invoice_status == "Processing" or invoice_status.startswith("Shipping"):
            invoice_color = "warning"
        elif invoice_status in ["Failed", "Rejected", "Hold"]:
            invoice_color = "danger"
        elif invoice_status == "Completed":
            invoice_color = "success"

        finish_time = invoice_db.get("finish_time")
        if finish_time:
            time_to_delivery = finish_time - int(ObjectId(invoice_db["_id"]).generation_time.timestamp())
            ttd_days = time_to_delivery // (60 * 60 * 24)
            ttd_hours = time_to_delivery % (60 * 60 * 24) // (60 * 60)
            ttd_minutes = time_to_delivery % (60 * 60 * 24) % (60 * 60) // 60
            ttd_format = "{0}D{1}H{2}M".format(ttd_days, ttd_hours, ttd_minutes)
        else:
            ttd_format = "N/A"

        invoice_row = [invoice_color, invoice_timestamp, ttd_format, invoice_db["_id"], invoice_db["jf_end"],
                       "{:,.02f}".format(invoice_db["order_total"]), invoice_db.get("character"),
                       invoice_db.get("marketeer"), invoice_status]

        invoice_table.append(invoice_row)
        if invoice_db.get("marketeer") == session["CharacterName"]:
            marketeer_invoice_table.append(invoice_row)
        if invoice_status in ["Not Processed", "Submitted"]:
            new_invoice_table.append(invoice_row)

    return render_template("ordering_admin.html", invoice_table=invoice_table, tax=tax, is_admin=is_admin,
                           marketeer_invoice_table=marketeer_invoice_table, new_invoice_table=new_invoice_table,
                           tax_corp=tax_corp)


@ordering.route("/custom")
@requires_sso("alliance")
def custom():
    pass
