"""
Generate a synthetic airline loyalty event log covering one week, with a
nationwide ground stop in the middle of it.

Everything here is made up: members, devices, bookings, and fraud. No real
airline, customer, or system data is used.

Usage:
    python generate_data.py            # writes data/events.csv and data/members.csv
"""

import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

SEED = 42
N_MEMBERS = 5000
START = datetime(2026, 11, 9, 0, 0)            # Monday
DAYS = 7
DISRUPTION_START = datetime(2026, 11, 13, 14, 0)  # Friday afternoon
DISRUPTION_END = DISRUPTION_START + timedelta(hours=5)
RECOVERY_END = DISRUPTION_END + timedelta(hours=48)  # rebooking / refund tail

STATES = ["AZ", "CA", "TX", "IL", "NY", "NJ", "CO", "FL", "WA", "GA", "VA", "OH"]
HUBS = ["IL", "CO", "TX", "NJ", "CA", "VA"]  # where stranded travelers end up

rng = random.Random(SEED)
OUT = Path(__file__).parent / "data"


def period(ts):
    if ts < DISRUPTION_START:
        return "baseline"
    if ts < DISRUPTION_END:
        return "ground_stop"
    if ts < RECOVERY_END:
        return "recovery"
    return "post_recovery"


def make_members():
    members = []
    for i in range(N_MEMBERS):
        members.append({
            "member_id": f"M{i:05d}",
            "home_state": rng.choice(STATES),
            "primary_device": f"D{i:05d}",
            "miles_balance": int(rng.lognormvariate(10.5, 0.9)),  # median ~36k
            "tenure_years": round(rng.uniform(0.2, 20), 1),
        })
    return members


def rand_ts(start, end):
    return start + timedelta(seconds=rng.randint(0, int((end - start).total_seconds())))


events = []
booking_counter = [0]


def new_booking():
    booking_counter[0] += 1
    return f"BK{booking_counter[0]:06d}"


def add(ts, m, etype, **kw):
    row = {
        "ts": ts,
        "member_id": m["member_id"],
        "event_type": etype,
        "channel": kw.get("channel", "app"),
        "device_id": kw.get("device_id", m["primary_device"]),
        "geo_state": kw.get("geo_state", m["home_state"]),
        "miles": kw.get("miles", 0),
        "usd": kw.get("usd", 0.0),
        "booking_ref": kw.get("booking_ref", ""),
        "target_account": kw.get("target_account", ""),
        "is_fraud": kw.get("is_fraud", False),
        "scenario": kw.get("scenario", "legit"),
    }
    events.append(row)


# ---------------------------------------------------------------- legit noise

def legit_activity(members):
    end = START + timedelta(days=DAYS)
    for m in members:
        # routine logins
        for _ in range(rng.randint(1, 6)):
            add(rand_ts(START, end), m, "login", channel=rng.choice(["app", "web"]))
        # occasional normal redemption
        if rng.random() < 0.06:
            ts = rand_ts(START, end)
            add(ts, m, "login")
            add(ts + timedelta(minutes=rng.randint(2, 20)), m, "redemption",
                miles=min(m["miles_balance"], rng.choice([12500, 25000, 30000, 45000])),
                booking_ref=new_booking())
        # forgot password, then nothing unusual
        if rng.random() < 0.02:
            add(rand_ts(START, end), m, "password_reset", channel=rng.choice(["app", "web"]))


def legit_disruption_activity(members):
    """Real travelers during and after the ground stop.

    These are the honest customers who generate noise: stranded in a hub city
    (new geo), logging in on a borrowed or hotel device, resetting passwords
    they forgot, rebooking with waivers, and asking for refunds.
    """
    affected = rng.sample(members, k=int(N_MEMBERS * 0.18))
    for m in affected:
        ts = rand_ts(DISRUPTION_START, DISRUPTION_END + timedelta(hours=6))
        stranded_geo = rng.choice(HUBS) if rng.random() < 0.6 else m["home_state"]
        device = m["primary_device"] if rng.random() < 0.85 else f"DX{rng.randint(0, 99999):05d}"
        add(ts, m, "login", geo_state=stranded_geo, device_id=device)
        reset = rng.random() < 0.25  # locked out at the gate, can't remember the password
        if reset:
            ts += timedelta(minutes=rng.randint(1, 10))
            add(ts, m, "password_reset", geo_state=stranded_geo, device_id=device)
        bk = new_booking()
        if rng.random() < 0.65:
            add(ts + timedelta(minutes=rng.randint(3, 40)), m, "waiver_rebook",
                geo_state=stranded_geo, device_id=device, booking_ref=bk)
        else:
            add(rand_ts(ts, RECOVERY_END), m, "refund_request",
                usd=round(rng.uniform(150, 900), 2), booking_ref=bk,
                geo_state=stranded_geo, device_id=device)
        # some stranded travelers burn miles on a hotel or a new flight
        if rng.random() < (0.45 if reset else 0.12):
            add(ts + timedelta(minutes=rng.randint(10, 90)), m, "redemption",
                miles=min(m["miles_balance"], rng.choice([10000, 25000, 35000])),
                geo_state=stranded_geo, device_id=device, booking_ref=new_booking())


# ---------------------------------------------------------------- fraud

MULES = [f"M9{i:04d}" for i in range(12)]  # receiving accounts outside the member base


def ato_phishing(m, ts, scenario="ato_phishing", low_and_slow=None):
    """Credential phishing ("your flight was canceled, confirm here"):
    new device + new geo, reset, contact change, then drain miles fast.

    About a third are "low and slow": same state as the member (VPN/proxy),
    a fresh device, and a wait of a few hours before cashing out."""
    if low_and_slow is None:
        low_and_slow = rng.random() < 0.33
    dev = f"DF{rng.randint(0, 39):03d}" if not low_and_slow else f"DS{rng.randint(0, 99999):05d}"
    geo = m["home_state"] if low_and_slow else rng.choice([s for s in STATES if s != m["home_state"]])
    if low_and_slow:
        scenario = scenario + "_low_and_slow"
    add(ts, m, "login", device_id=dev, geo_state=geo, channel="web", is_fraud=True, scenario=scenario)
    ts += timedelta(minutes=rng.randint(1, 6))
    add(ts, m, "password_reset", device_id=dev, geo_state=geo, channel="web", is_fraud=True, scenario=scenario)
    ts += timedelta(minutes=rng.randint(1, 8))
    add(ts, m, "contact_update", device_id=dev, geo_state=geo, channel="web", is_fraud=True, scenario=scenario)
    ts += timedelta(hours=rng.randint(2, 8)) if low_and_slow else timedelta(minutes=rng.randint(2, 25))
    drain = int(m["miles_balance"] * (rng.uniform(0.3, 0.65) if low_and_slow else rng.uniform(0.7, 1.0)))
    if rng.random() < 0.6:
        add(ts, m, "miles_transfer", device_id=dev, geo_state=geo, channel="web", miles=drain,
            target_account=rng.choice(MULES), is_fraud=True, scenario=scenario)
    else:
        add(ts, m, "redemption", device_id=dev, geo_state=geo, channel="web", miles=drain,
            booking_ref=new_booking(), is_fraud=True, scenario=scenario)


def fake_support_call(m, ts):
    """Fake 'airline support' number found via search ads. The scammer then
    calls the real contact center as the customer to reset access."""
    scenario = "fake_support_social_engineering"
    add(ts, m, "password_reset", channel="call_center", geo_state=m["home_state"],
        device_id="CALLCTR", is_fraud=True, scenario=scenario)
    ts += timedelta(minutes=rng.randint(5, 50))
    dev = f"DF{rng.randint(0, 39):03d}"
    add(ts, m, "login", device_id=dev, geo_state=m["home_state"], channel="web", is_fraud=True, scenario=scenario)
    ts += timedelta(minutes=rng.randint(2, 15))
    add(ts, m, "miles_transfer", device_id=dev, channel="web", geo_state=m["home_state"],
        miles=int(m["miles_balance"] * rng.uniform(0.5, 0.95)),
        target_account=rng.choice(MULES), is_fraud=True, scenario=scenario)


def waiver_refund_abuse(m, ts):
    """Same booking used twice: take the free waiver rebook AND file a refund."""
    scenario = "waiver_refund_abuse"
    bk = new_booking()
    add(ts, m, "waiver_rebook", booking_ref=bk, is_fraud=True, scenario=scenario)
    add(ts + timedelta(hours=rng.randint(2, 30)), m, "refund_request", booking_ref=bk,
        usd=round(rng.uniform(250, 1400), 2), is_fraud=True, scenario=scenario)
    if rng.random() < 0.4:  # some try twice
        add(ts + timedelta(hours=rng.randint(31, 44)), m, "refund_request", booking_ref=bk,
            usd=round(rng.uniform(250, 1400), 2), channel="web", is_fraud=True, scenario=scenario)


def inject_fraud(members):
    pool = rng.sample(members, k=260)
    i = 0
    # baseline: a steady trickle of ATO
    for m in pool[i:i + 25]:
        ato_phishing(m, rand_ts(START, DISRUPTION_START))
    i += 25
    # disruption + recovery: scammers follow the stranded travelers
    for m in pool[i:i + 110]:
        ato_phishing(m, rand_ts(DISRUPTION_START, RECOVERY_END))
    i += 110
    for m in pool[i:i + 45]:
        fake_support_call(m, rand_ts(DISRUPTION_START, RECOVERY_END))
    i += 45
    for m in pool[i:i + 60]:
        waiver_refund_abuse(m, rand_ts(DISRUPTION_START, DISRUPTION_END + timedelta(hours=12)))
    i += 60
    # post-recovery tail
    for m in pool[i:i + 20]:
        ato_phishing(m, rand_ts(RECOVERY_END, START + timedelta(days=DAYS)))


def main():
    members = make_members()
    legit_activity(members)
    legit_disruption_activity(members)
    inject_fraud(members)

    df = pd.DataFrame(events).sort_values("ts").reset_index(drop=True)
    df.insert(0, "event_id", [f"E{i:07d}" for i in range(len(df))])
    df["period"] = df["ts"].map(period)

    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "events.csv", index=False)
    pd.DataFrame(members).to_csv(OUT / "members.csv", index=False)
    print(f"Wrote {len(df):,} events for {N_MEMBERS:,} members "
          f"({df.is_fraud.sum()} fraud events) to {OUT}/")


if __name__ == "__main__":
    main()
