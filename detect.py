"""
Score every "money-out" event (miles redemption, miles transfer, refund
request) against a small set of explainable fraud rules, build a triage
queue, and report how the week went, with the ground stop called out.

Runs two rule configurations side by side:
  * standard        - the same rules all week
  * disruption_mode - during the ground stop and recovery window, stop
                      penalizing logins from hub cities on a member's own
                      device (stranded travelers), so analysts are not
                      buried in honest customers

Usage:
    python detect.py            # reads data/, writes reports/
"""

from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from generate_data import DISRUPTION_START, DISRUPTION_END, RECOVERY_END, HUBS

BASE = Path(__file__).parent
DATA, REPORTS = BASE / "data", BASE / "reports"

MONEY_OUT = {"redemption", "miles_transfer", "refund_request"}
ALERT_THRESHOLD = 50
P1_THRESHOLD = 80
MILE_VALUE_USD = 0.012       # rough market value of one mile
HANDLE_MINUTES = 20          # average analyst time per alert
PRODUCTIVE_HOURS = 6.5       # per analyst per day

RULES = {
    "R1_recent_credential_change": ("Password reset or contact change in the prior 60 min", 40),
    "R2_new_device": ("Device first seen on this account in the last 24 hours", 15),
    "R3_new_geo": ("Activity from outside the member's home state", 10),
    "R4_call_center_reset": ("Contact-center password reset in the prior 2 hours", 25),
    "R5_mule_account": ("Receiving account got transfers from 3+ members in 24h", 35),
    "R6_balance_drain": ("Takes 70%+ of the member's miles balance", 20),
    "R7_duplicate_claim": ("Booking already rebooked on a waiver or refunded", 50),
    "R8_shared_device": ("Device already used by 3+ different members", 25),
}


def in_disruption(ts):
    return DISRUPTION_START <= ts < RECOVERY_END


def score_events(events, members, disruption_mode=False):
    home = members.set_index("member_id")["home_state"].to_dict()
    primary = members.set_index("member_id")["primary_device"].to_dict()
    balance = members.set_index("member_id")["miles_balance"].to_dict()

    first_seen = defaultdict(dict)       # member -> {device: first ts}
    device_members = defaultdict(set)    # device -> members seen so far
    recent = defaultdict(list)           # member -> [(ts, event_type, channel)]
    transfers_in = defaultdict(list)     # target -> [(ts, member)]
    booking_history = defaultdict(list)  # booking_ref -> [event_type]

    rows = []
    for e in events.itertuples(index=False):
        m, ts = e.member_id, e.ts
        hits = []

        if e.event_type in MONEY_OUT:
            window = [r for r in recent[m] if ts - r[0] <= timedelta(hours=2)]
            if any(t in ("password_reset", "contact_update") and ts - r_ts <= timedelta(minutes=60)
                   for r_ts, t, _ in window):
                hits.append("R1_recent_credential_change")
            if any(t == "password_reset" and ch == "call_center" for _, t, ch in window):
                hits.append("R4_call_center_reset")

            fs = first_seen[m].get(e.device_id, ts)
            new_device = e.device_id != primary[m] and ts - fs <= timedelta(hours=24)
            new_geo = e.geo_state != home[m]
            stranded = (disruption_mode and in_disruption(ts)
                        and e.geo_state in HUBS and e.device_id == primary[m])
            if new_device:
                hits.append("R2_new_device")
            if new_geo and not stranded:
                hits.append("R3_new_geo")

            if e.event_type == "miles_transfer" and e.target_account:
                senders = {s for t, s in transfers_in[e.target_account] if ts - t <= timedelta(hours=24)}
                if len(senders - {m}) >= 2:  # this member makes 3+
                    hits.append("R5_mule_account")
            if e.miles and e.miles >= 0.7 * balance[m]:
                hits.append("R6_balance_drain")
            if e.event_type == "refund_request" and e.booking_ref and booking_history[e.booking_ref]:
                hits.append("R7_duplicate_claim")
            if len(device_members[e.device_id] - {m}) >= 3 and e.device_id != "CALLCTR":
                hits.append("R8_shared_device")

            score = sum(RULES[h][1] for h in hits)
            rows.append({
                "event_id": e.event_id, "ts": ts, "member_id": m, "event_type": e.event_type,
                "period": e.period, "miles": e.miles, "usd": e.usd,
                "value_usd": round(e.miles * MILE_VALUE_USD + e.usd, 2),
                "score": score, "rules_hit": ";".join(hits),
                "alert": score >= ALERT_THRESHOLD,
                "priority": "P1" if score >= P1_THRESHOLD else ("P2" if score >= ALERT_THRESHOLD else ""),
                "is_fraud": e.is_fraud, "scenario": e.scenario,
            })

        # update state after scoring so an event never counts against itself
        recent[m].append((ts, e.event_type, e.channel))
        first_seen[m].setdefault(e.device_id, ts)
        device_members[e.device_id].add(m)
        if e.event_type == "miles_transfer" and e.target_account:
            transfers_in[e.target_account].append((ts, m))
        if e.booking_ref:
            booking_history[e.booking_ref].append(e.event_type)

    return pd.DataFrame(rows)


def summarize(scored):
    out = []
    for p in ["baseline", "ground_stop", "recovery", "post_recovery", "ALL"]:
        s = scored if p == "ALL" else scored[scored.period == p]
        alerts = s[s.alert]
        tp = alerts.is_fraud.sum()
        fraud = s.is_fraud.sum()
        hours = len(alerts) * HANDLE_MINUTES / 60
        out.append({
            "period": p,
            "money_out_events": len(s),
            "alerts": len(alerts),
            "p1_alerts": (alerts.priority == "P1").sum(),
            "true_positives": int(tp),
            "false_positives": int(len(alerts) - tp),
            "precision": round(tp / len(alerts), 2) if len(alerts) else None,
            "recall": round(tp / fraud, 2) if fraud else None,
            "prevented_loss_usd": round(alerts[alerts.is_fraud].value_usd.sum()),
            "missed_loss_usd": round(s[s.is_fraud & ~s.alert].value_usd.sum()),
            "analyst_hours": round(hours, 1),
        })
    return pd.DataFrame(out)


def daily_staffing(scored):
    a = scored[scored.alert].copy()
    a["date"] = a.ts.dt.date
    d = a.groupby("date").size().rename("alerts").reset_index()
    d["analyst_hours"] = (d.alerts * HANDLE_MINUTES / 60).round(1)
    d["analysts_needed"] = (d.analyst_hours / PRODUCTIVE_HOURS).round(1)
    return d


def chart(std, dm, path):
    fig, ax = plt.subplots(figsize=(11, 4.2))
    for df, label, color in [(std, "Standard rules", "#9aa5b1"), (dm, "Disruption mode", "#1f3864")]:
        h = df[df.alert].set_index("ts").resample("1h").size()
        ax.plot(h.index, h.values, label=label, color=color, linewidth=1.8)
    ax.axvspan(DISRUPTION_START, DISRUPTION_END, color="#e4572e", alpha=0.18, label="Ground stop")
    ax.axvspan(DISRUPTION_END, RECOVERY_END, color="#f2a541", alpha=0.12, label="Recovery window")
    ax.set_title("Fraud alerts per hour, synthetic airline loyalty program", loc="left", fontsize=12)
    ax.set_ylabel("Alerts")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=4, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)


def md_table(df):
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        lines.append("| " + " | ".join("" if pd.isna(v) else f"{v:,}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v) for v in r) + " |")
    return "\n".join(lines)


def main():
    events = pd.read_csv(DATA / "events.csv", parse_dates=["ts"])
    members = pd.read_csv(DATA / "members.csv")
    REPORTS.mkdir(exist_ok=True)

    std = score_events(events, members, disruption_mode=False)
    dm = score_events(events, members, disruption_mode=True)

    s_std, s_dm = summarize(std), summarize(dm)
    staffing = daily_staffing(dm)

    queue = dm[dm.alert].sort_values(["priority", "score", "ts"], ascending=[True, False, True])
    queue.drop(columns=["is_fraud", "scenario"]).to_csv(REPORTS / "triage_queue.csv", index=False)
    chart(std, dm, REPORTS / "alerts_per_hour.png")

    by_rule = (dm[dm.alert].assign(rule=dm.rules_hit.str.split(";")).explode("rule")
               .groupby("rule").agg(alerts=("event_id", "size"), fraud=("is_fraud", "sum")))
    by_rule["precision_when_present"] = (by_rule.fraud / by_rule.alerts).round(2)
    by_rule.insert(0, "description", [RULES[r][0] for r in by_rule.index])
    by_rule.insert(1, "points", [RULES[r][1] for r in by_rule.index])

    caught = dm[dm.is_fraud].groupby("scenario").agg(fraud_events=("alert", "size"), caught=("alert", "sum"))
    caught["recall"] = (caught.caught / caught.fraud_events).round(2)

    fp_drop = int(s_std.loc[s_std.period == "ALL", "false_positives"].iloc[0]
                  - s_dm.loc[s_dm.period == "ALL", "false_positives"].iloc[0])

    report = f"""# Run summary

Synthetic data only. Numbers show how the approach behaves, not real airline results.

## Disruption mode vs. standard rules
Disruption mode removed **{fp_drop}** false positives over the week (honest travelers stranded in hub cities on their own phones).

### Standard rules
{md_table(s_std)}

### Disruption mode
{md_table(s_dm)}

## Recall by fraud scenario (disruption mode)
{md_table(caught.reset_index())}

## Rule performance (disruption mode, alerted events)
{md_table(by_rule.reset_index())}

## Daily staffing view
Assumes {HANDLE_MINUTES} minutes per alert and {PRODUCTIVE_HOURS} productive hours per analyst per day.

{md_table(staffing)}

![Alerts per hour](alerts_per_hour.png)
"""
    (REPORTS / "summary.md").write_text(report)
    print(s_dm.to_string(index=False))
    print(f"\nFalse positives removed by disruption mode: {fp_drop}")
    print(caught.to_string())
    print(staffing.to_string(index=False))


if __name__ == "__main__":
    main()
