import json
import time
import calendar

from flask import Blueprint, render_template, g, redirect, url_for, request, flash
from pymongo import ReturnDocument

from views.auth import requires_sso, auth_crest
from helpers import caches, background

security = Blueprint("security", __name__, template_folder="templates")


@security.route("/", methods=["GET", "POST"])
@requires_sso("security_officer")
def home():
    with open("configs/base.json", "r") as base_config_file:
        base_config = json.load(base_config_file)

    if request.form.get("action") == "force_api_validation":
        background.api_validation.delay()
    elif request.form.get("action") == "force_unlock":
        g.mongo.db.preferences.update_one({"_id": "updates"}, {"$unset": {"api_validation": True}})

    caches.security_characters()

    id_to_name = {}
    name_to_id = {}
    for user_info in g.mongo.db.users.find({"corporation_id": base_config["corporation_id"]}):
        if g.mongo.db.security_characters.find_one({"_id": user_info["character_id"]}):
            id_to_name[user_info["_id"]] = user_info["character_name"]
            name_to_id[user_info["character_name"]] = user_info["_id"]

    api_table = []
    api_info_table = []
    characters = set()
    id_to_chars = {}
    char_to_main = {}
    main_to_chars = {}
    invalid_characters = set()
    for api_user in g.mongo.db.api_keys.find({"_id": {"$in": list(id_to_name.keys())}}):
        user_characters = []
        invalid = False
        for key in api_user["keys"]:
            user_characters.append(key["character_name"])
            char_to_main[key["character_name"]] = id_to_name[api_user["_id"]]
            if not key.get("valid", True):
                invalid = True
                invalid_characters.add(key["character_name"])
            api_info_table.append([
                key.get("valid", True),
                api_user["_id"],
                key["character_name"],
                id_to_name[api_user["_id"]],
                key.get("corporation_name", "unknown"),
                key.get("alliance_name", "unknown")
            ])
        api_table.append([invalid, api_user["_id"], id_to_name[api_user["_id"]], ", ".join(user_characters)])
        id_to_chars[api_user["_id"]] = user_characters
        main_to_chars[id_to_name[api_user["_id"]]] = user_characters + [id_to_name[api_user["_id"]]]
        characters.update(user_characters)

    unknown_vacation = []
    valid_vacation = []
    expired_vacation = []
    for vacation_user in g.mongo.db.personals.find({"vacation_date": {"$exists": True}}):
        # IDs not in id_to_chars have no associated APIs
        if vacation_user["_id"] in id_to_chars:
            valid_time_formats = ["%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"]
            return_time = -1
            for current_time_format in valid_time_formats:
                try:
                    return_time = calendar.timegm(time.strptime(vacation_user["vacation_date"], current_time_format))
                except ValueError:
                    pass
            if return_time == -1:
                unknown_vacation.extend(id_to_chars[vacation_user["_id"]])
            elif return_time < time.time():
                expired_vacation.extend(id_to_chars[vacation_user["_id"]])
            else:
                valid_vacation.extend(id_to_chars[vacation_user["_id"]])

    time_format = "%Y-%m-%d %H:%M:%S"
    missing_apis = []
    invalid_apis = []
    inactivity_30_days = []
    inactivity_60_days = []
    active = []
    valid_vacation_table = []
    expired_vacation_table = []
    unknown_vacation_table = []
    for corp_character in g.mongo.db.security_characters.find():
        # Validator
        color = ""
        if corp_character["name"] in invalid_characters:
            color = "danger"
        elif corp_character["name"] in unknown_vacation:
            color = "info"
        elif corp_character["name"] in expired_vacation:
            color = "warning"
        elif corp_character["name"] in valid_vacation:
            color = "success"

        api_row = [
            color,
            name_to_id.get(char_to_main.get(corp_character["name"])),
            corp_character["name"],
            char_to_main.get(corp_character["name"]),
            time.strftime(time_format, time.gmtime(corp_character["join_time"])),
            time.strftime(time_format, time.gmtime(corp_character["log_on_time"])),
            time.strftime(time_format, time.gmtime(corp_character["log_off_time"]))
        ]

        # Sorter
        if corp_character["name"] not in characters:
            missing_apis.append(api_row)
        if corp_character["name"] in invalid_characters:
            invalid_apis.append(api_row)

        if color == "success":
            pass
        elif corp_character["log_on_time"] < time.time() - 5184000:
            evemail_setting = g.mongo.db.preferences.find_one({"_id": "inactivity_mail"})
            evemail_subject = ""
            evemail_lines = ""
            if evemail_setting:
                evemail_subject = evemail_setting.get("subject", "")
                evemail_lines = evemail_setting.get("text", "")
            # Defaults
            evemail_subject = "Automated Message" if not evemail_subject.strip() else evemail_subject.strip()
            evemail_lines = "Empty Message" if not evemail_lines.strip() else evemail_lines.strip()

            evemail_call = "CCPEVE.sendMail({0}, '{1}', '{2}')".format(
                corp_character["_id"],
                evemail_subject.strip(),
                "\\n".join([x.strip() for x in evemail_lines.format(character=corp_character["name"]).split("\n")])
            )
            inactivity_60_days.append(api_row + [evemail_call])
        elif corp_character["log_on_time"] < time.time() - 2592000:
            inactivity_30_days.append(api_row)
        else:
            active.append(api_row)

        if api_row[0] == "success":
            valid_vacation_table.append(api_row)
        elif api_row[0] == "info":
            unknown_vacation_table.append(api_row)
        elif api_row[0] == "warning":
            expired_vacation_table.append(api_row)

    # Determine inactive characters with active characters on same account
    for character_30 in inactivity_30_days:
        # If color is not danger and a main is associated with the character
        if character_30[0] != "danger" and char_to_main.get(character_30[3]):
            # if at least one character associated with the main is active
            if set(main_to_chars[char_to_main.get(character_30[3])]) & set([x[2] for x in active]):
                character_30[0] = "active"
    for character_60 in inactivity_60_days:
        # If color is not danger and a main is associated with the character
        if character_60[0] != "danger" and char_to_main.get(character_60[3]):
            # if at least one character associated with the main is active
            if set(main_to_chars[char_to_main.get(character_60[3])]) & set([x[2] for x in active]):
                character_60[0] = "active"

    missing_count = len(missing_apis)
    all_count = g.mongo.db.security_characters.count()

    with open("configs/base.json") as base_config_file:
        base_config = json.load(base_config_file)
        trust_call = "CCPEVE.requestTrust('{0}');".format(base_config["dashboard_url"])

    # Check api validation status
    updates = g.mongo.db.preferences.find_one({"_id": "updates"})
    last_validation = updates.get("api_validation", "Has never run") if updates else "Has never run"
    validation_disable = "disabled" if last_validation.startswith("running") else ""
    force_disable = "disabled" if not last_validation.startswith("running") else ""

    return render_template("security.html", api_table=api_table, missing_apis=missing_apis, missing_count=missing_count,
                           all_count=all_count, inactivity_30_days=inactivity_30_days,
                           inactivity_60_days=inactivity_60_days, active=active,
                           valid_vacation_table=valid_vacation_table, unknown_vacation_table=unknown_vacation_table,
                           expired_vacation_table=expired_vacation_table, last_validation=last_validation,
                           trust_call=trust_call, invalid_apis=invalid_apis, validation_disable=validation_disable,
                           force_disable=force_disable, api_info_table=api_info_table)


@security.route("/user/<path:site_id>", methods=["GET", "POST"])
@requires_sso("security_officer")
def user(site_id=""):
    if not site_id:
        flash("No site id given.", "error")
        return redirect(url_for("security.home"))
    else:
        site_id = site_id.strip()
        if request.form.get("action") == "refresh":
            auth_crest(site_id, True)
            api_key_db = g.mongo.db.api_keys.find_one({"_id": site_id})
            if api_key_db:
                caches.api_keys([(x["key_id"], x["vcode"]) for x in api_key_db["keys"]], False, site_id, False)

    error_list = []
    # Users
    if request.form.get("action") == "delete":
        if request.form.get("confirm") == site_id:
            g.mongo.db.users.delete_one({"_id": site_id})
            flash("Account Deleted", "success")
            return redirect(url_for("security.home"))
        else:
            flash("Site ID didn't match.", "error")
    user_info = g.mongo.db.users.find_one({"_id": site_id})
    # Vacation
    if request.form.get("action") == "vacation":
        vacation_db = g.mongo.db.personals.find_one_and_update(
            {"_id": site_id}, {"$unset": {"vacation": True, "vacation_date": True}},
            return_document=ReturnDocument.AFTER)
        flash("Vacation reset.", "success")
    else:
        vacation_db = g.mongo.db.personals.find_one({"_id": site_id})
    if not user_info:
        flash("No user found.", "error")
        return redirect(url_for("security.home"))
    # APIs
    if request.form.get("action") == "add":
        error_list = caches.api_keys([(request.form.get("key_id"), request.form.get("vcode"))],
                                     dashboard_id=site_id)
    if request.form.get("action") == "remove":
        user_apis = g.mongo.db.api_keys.find_one_and_update({"_id": site_id},
                                                            {
                                                                "$pull": {
                                                                    "keys": {"key_id": int(request.form.get("key_id"))}
                                                                }
                                                            },
                                                            return_document=ReturnDocument.AFTER)
        flash("Removed key id {0}".format(request.form.get("key_id")), "success")
    elif request.form.get("action") == "remove_old":
        user_apis = g.mongo.db.api_keys.find_one_and_update({"_id": site_id},
                                                            {
                                                                "$pull": {
                                                                    "old_keys": {
                                                                        "key_id": int(request.form.get("key_id"))}
                                                                }
                                                            }, return_document=ReturnDocument.AFTER)
        flash("Removed old key id {0}".format(request.form.get("key_id")), "success")
    else:
        user_apis = g.mongo.db.api_keys.find_one({"_id": site_id})

    api_table = []
    old_api_table = []
    id_list = []
    affiliation_table = []
    if user_apis:
        for api_key in user_apis["keys"]:
            api_table.append([api_key["character_name"], api_key["character_id"], api_key["key_id"], api_key["vcode"],
                              api_key["cached_str"], api_key.get("valid", True)])
            id_list.append(api_key["character_id"])
            affiliation_table.append([api_key["character_name"], api_key["corporation_name"], api_key["alliance_name"]])
        if user_apis.get("old_keys"):
            for key in user_apis["old_keys"]:
                old_api_table.append([key["key_id"], key["vcode"],
                                      time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(key["delete_time"]))])

    vacation_text = vacation_db.get("vacation") if vacation_db else None
    vacation_date = vacation_db.get("vacation_date") if vacation_db else None

    time_format = "%Y-%m-%d %H:%M:%S"

    location_table = []
    for security_character in g.mongo.db.security_characters.find({"_id": {"$in": id_list}}):
        location_table.append([security_character["name"], security_character["last_location_str"],
                               security_character["last_ship_str"],
                               time.strftime(time_format, time.gmtime(security_character["log_on_time"])),
                               time.strftime(time_format, time.gmtime(security_character["log_off_time"]))])

    user_table = [site_id, user_info["character_name"],
                  time.strftime(time_format, time.gmtime(user_info.get("last_sign_on", 0)))]

    with open("configs/base.json", "r") as base_config_file:
        base_config = json.load(base_config_file)
    image = base_config["image_server"] + "/Character/" + str(user_info["character_id"]) + "_256.jpg"

    return render_template("security_user.html", api_table=api_table, user_table=user_table, image=image,
                           site_log_in=time.strftime(time_format, time.gmtime(user_info.get("last_sign_on", 0))),
                           site_id=site_id, character_name=user_info["character_name"], location_table=location_table,
                           vacation_text=vacation_text, vacation_date=vacation_date,
                           affiliation_table=affiliation_table, error_list=error_list,
                           sso_alliance=user_info["alliance_name"], sso_corporation=user_info["corporation_name"],
                           old_api_table=old_api_table)


@security.route("/settings", methods=["GET", "POST"])
@requires_sso("security_officer")
def settings():
    if request.form.get("action") == "save":
        g.mongo.db.preferences.update({"_id": "inactivity_mail"},
                                      {
                                          "subject": request.form.get("evemail_subject", ""),
                                          "text": request.form.get("evemail_text")
                                      },
                                      upsert=True)

    inactivity_mail = g.mongo.db.preferences.find_one({"_id": "inactivity_mail"})
    if not inactivity_mail:
        inactivity_mail = {
            "subject": "",
            "text": ""
        }

    return render_template("security_settings.html", evemail_subject=inactivity_mail.get("subject", ""),
                           evemail_text=inactivity_mail.get("text", ""))
