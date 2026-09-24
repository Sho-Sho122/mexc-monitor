CORE = "CORE_B1_PULLBACK_OPPOSITE"


def membership(event):
    pb = event["has_pullback"]
    opposite = event["crowding_state"] == "OPPOSITE"
    core = pb and opposite
    low = event["persistence_category"] == "LOW"
    asset = event["asset_class"] in ("CRYPTO_NATIVE", "COMMODITY_LINKED")
    conditions = {
        CORE: core,
        "CH_B1_PERSISTENCE_LOW": core and low,
        "CH_B1_PRIMARY_ASSET": core and asset,
        "CH_B1_PERSISTENCE_ASSET": core and low and asset,
        "CTRL_PULLBACK_ANY": pb,
        "CTRL_OPPOSITE_ANY": opposite,
        "CTRL_OPPOSITE_NON_PULLBACK": opposite and not pb,
        "CTRL_PULLBACK_MATCH": pb and event["crowding_state"] == "MATCH",
        "CTRL_TREND_VOLUME": event["has_trend_volume"],
        "CTRL_OI_FUNDING": event["has_oi_funding"],
        "CTRL_HIGH_EDGE": event["has_high_edge"],
    }
    return [{"event_id": event["event_id"], "model_id": key, "eligible": value,
             "reason": "MATCHED" if value else "CONDITION_NOT_MET",
             "spec_version": event["spec_version"],
             "decision_created_at_jst": event["decision_created_at_jst"]}
            for key, value in conditions.items()]

