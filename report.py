"""
Weekly Recruiting Report
Pulls open roles from Greenhouse, classifies them by stage, and sends an HTML email.
"""

import os
import json
import base64
import datetime
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

GH_CLIENT_ID      = os.environ.get("GH_CLIENT_ID", "")
GH_CLIENT_SECRET  = os.environ.get("GH_CLIENT_SECRET", "")
GH_USER_ID        = os.environ.get("GH_USER_ID", "")  # Your Greenhouse Site Admin user ID
SENDER_EMAIL      = os.environ.get("SENDER_EMAIL", "you@code.org")
RECIPIENTS        = os.environ.get("RECIPIENTS", "exec-team@code.org").split(",")
GMAIL_TOKEN_FILE  = "token.json"
GMAIL_CREDS_FILE  = "credentials.json"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

# ── Stage mapping ─────────────────────────────────────────────────────────────
# Map your Greenhouse interview stage names to Early / Mid / Late.
# Lowercase, partial matches work (e.g. "informational" matches "Informational Interview").
# Add or rename stages here as your process evolves.

STAGE_MAP = {
    "early": [
        "shortlist",
        "informational",
        "hiring manager review",
        "application review",
        "agency",
        "sourcing",
        "resume review",
        "phone screen",
    ],
    "mid": [
        "interview loop",
        "panel",
        "homework",
        "take-home",
        "skills assessment",
        "technical",
    ],
    "late": [
        "final interview",
        "reference",
        "offer",
        "background",
        "executive interview",
    ],
}

# ── Overrides (edit config.json each week before the script runs) ─────────────

def load_config():
    """Load config.json, returning overrides and job_id_map."""
    try:
        with open("config.json") as f:
            data = json.load(f)
            return data.get("overrides", {}), data.get("job_id_map", {})
    except FileNotFoundError:
        return {}, {}

# ── Greenhouse API ────────────────────────────────────────────────────────────

GH_BASE = "https://harvest.greenhouse.io/v3"
GH_AUTH_URL = "https://auth.greenhouse.io/token"

def get_gh_token():
    """Fetch a fresh OAuth access token from Greenhouse. Valid for 1 hour."""
    r = requests.post(
        GH_AUTH_URL,
        auth=(GH_CLIENT_ID, GH_CLIENT_SECRET),
        data={"grant_type": "client_credentials", "sub": GH_USER_ID},
    )
    r.raise_for_status()
    return r.json()["access_token"]

def gh_get(endpoint, params=None, token=None):
    """Make a paginated GET request to the Greenhouse Harvest v3 API."""
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    url = f"{GH_BASE}{endpoint}"
    while url:
        r = requests.get(url, headers=headers, params=params)
        if not r.ok:
            print(f"  API error {r.status_code} on {r.url}")
            try:
                print(f"  Response: {r.json()}")
            except Exception:
                print(f"  Response: {r.text}")
            r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        # v3 uses cursor-based pagination via Link header
        link = r.headers.get("Link", "")
        url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        params = None  # cursor is embedded in the next URL
    return results


def get_open_jobs(token):
    """Return all open jobs."""
    return gh_get("/jobs", params={"status": "open"}, token=token)


def get_job_openings(job_id, token):
    """
    Fetch open openings for a job using the v3 /openings endpoint.
    Filters by job_ids and open=true (v3 uses boolean instead of status string).
    Returns a formatted label like '#324' or '#331 & #338', or None if unavailable.
    """
    try:
        openings = gh_get("/openings", params={"job_ids": job_id, "open": "true"}, token=token)
        if not openings:
            return None
        # opening_id is the human-readable custom ID; fall back to system id
        ids = [str(o.get("opening_id") or o.get("id", "")) for o in openings if o.get("opening_id") or o.get("id")]
        if not ids:
            return None
        return " & ".join(f"#{oid}" for oid in ids)
    except Exception as e:
        print(f"  [openings warning] {e}")
        return None


def get_active_applications(job_id, token):
    """Return active (non-rejected, non-hired) applications for a job."""
    # v3 uses plural "job_ids" as the parent resource filter, not "job_id"
    # Status values in v3: "active", "hired", "rejected" -- filter client-side to be safe
    apps = gh_get("/applications", params={"job_ids": job_id}, token=token)
    return [a for a in apps if a.get("status") not in ("rejected", "hired")]


def get_recent_offers(token, days=7):
    """Return offers accepted in the last N days."""
    # v3 date filter syntax uses pipe operator: field=gte|value
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    offers = gh_get("/offers", params={"resolved_at": f"gte|{cutoff}"}, token=token)
    return [o for o in offers if o.get("status") == "accepted"]


def get_new_jobs(token, days=7):
    """Return jobs opened in the last N days."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    jobs = gh_get("/jobs", params={"status": "open"}, token=token)
    return [
        j for j in jobs
        if j.get("opened_at") and
        datetime.datetime.fromisoformat(j["opened_at"].replace("Z", "+00:00")).replace(tzinfo=None) >= cutoff
    ]

# ── Stage classification ──────────────────────────────────────────────────────

def classify_stage(stage_name):
    """Map a Greenhouse stage name to Early / Mid / Late."""
    name = stage_name.lower()
    for bucket, keywords in STAGE_MAP.items():
        if any(kw in name for kw in keywords):
            return bucket.capitalize()
    return "Early"  # default if unknown


def most_advanced_stage(applications):
    """
    Given a list of applications, return the stage bucket AND the most
    advanced stage name within that bucket (Late > Mid > Early).
    v3 exposes stage as a plain string field: app["stage_name"]
    """
    priority = {"Late": 3, "Mid": 2, "Early": 1}
    # Collect all (bucket, stage_name) pairs
    stages = []
    for app in applications:
        stage_name = app.get("stage_name") or ""
        if not stage_name:
            continue
        bucket = classify_stage(stage_name)
        stages.append((bucket, stage_name))

    if not stages:
        return "Early", ""

    # Sort by bucket priority descending, then pick the most advanced stage name
    # within the winning bucket based on keyword order in STAGE_MAP
    best_bucket = max(stages, key=lambda x: priority.get(x[0], 0))[0]
    bucket_stages = [s for b, s in stages if b == best_bucket]

    # Pick the stage name that appears latest in the STAGE_MAP keyword list
    # (lower index = earlier stage, so we want the one matching the latest keyword)
    keyword_order = STAGE_MAP[best_bucket.lower()]
    def stage_rank(stage_name):
        name = stage_name.lower()
        for i, kw in enumerate(keyword_order):
            if kw in name:
                return i
        return 999
    best_stage_name = max(bucket_stages, key=stage_rank)
    return best_bucket, best_stage_name

# ── Build report data ─────────────────────────────────────────────────────────

def build_report():
    overrides, job_id_map = load_config()
    token = get_gh_token()
    jobs = get_open_jobs(token)

    total_roles = len(jobs)
    # Headcount will be counted from actual open openings per job below
    total_headcount = 0

    buckets = {"Early": [], "Mid": [], "Late": []}

    for job in jobs:
        job_id   = job["id"]
        job_name = job["name"]
        openings = job.get("number_of_openings", 1)

        # Try to get opening IDs from Greenhouse v2 openings endpoint
        opening_label = get_job_openings(job_id, token)
        job_ids_label = job_id_map.get(str(job_id)) or opening_label or f"#{job_id}"

        # Get applications and classify
        apps = get_active_applications(job_id, token)



        if str(job_id) in overrides:
            bucket = overrides[str(job_id)]["stage"]
            stage_name = overrides[str(job_id)].get("stage_label", "")
        elif apps:
            bucket, stage_name = most_advanced_stage(apps)
        else:
            bucket = "Early"
            stage_name = "No active candidates"

        # Count headcount from actual openings if available, else number_of_openings
        opening_count = len([o for o in ([] if not opening_label else opening_label.split(" & "))]) if opening_label else job.get("number_of_openings", 1)
        total_headcount += opening_count

        buckets[bucket].append({
            "name": job_name,
            "ids_label": job_ids_label,
            "stage_name": stage_name,
            "openings": opening_count,
        })

    # Offers accepted this week
    recent_offers = get_recent_offers(token, days=7)
    offer_acceptances = []
    for offer in recent_offers:
        candidate = offer.get("candidate", {})
        first = candidate.get("first_name", "")
        last_initial = (candidate.get("last_name") or " ")[0] + "."
        job_name = offer.get("job", {}).get("name", "Unknown Role")
        job_id_str = f"#{offer.get('job', {}).get('id', '')}"
        start_date = offer.get("starts_at", "")
        if start_date:
            try:
                start_date = datetime.datetime.fromisoformat(
                    start_date.replace("Z", "+00:00")
                ).strftime("%-m/%-d")
            except Exception:
                pass
        offer_acceptances.append({
            "name": f"{first} {last_initial}",
            "role": job_name,
            "job_id": job_id_str,
            "start_date": start_date,
        })

    # New roles posted this week
    new_jobs = get_new_jobs(token, days=7)
    new_roles = []
    for j in new_jobs:
        new_roles.append({
            "name": j["name"],
            "ids_label": _format_job_ids(j),
            "openings": j.get("number_of_openings", 1),
        })

    return {
        "total_roles": total_roles,
        "total_headcount": total_headcount,
        "buckets": buckets,
        "offer_acceptances": offer_acceptances,
        "new_roles": new_roles,
        "generated_at": datetime.datetime.now().strftime("%B %d, %Y"),
    }


def _format_job_ids(job):
    """Format job ID(s) as '#324' or '#331 & #338' for multi-opening roles."""
    openings = job.get("number_of_openings", 1)
    base_id = job["id"]
    if openings == 1:
        return f"#{base_id}"
    # Greenhouse doesn't expose individual opening IDs via the basic jobs endpoint,
    # so we approximate by using the base ID + sequential suffixes.
    # If you have specific opening IDs, update this function.
    ids = [f"#{base_id + i}" for i in range(openings)]
    return " & ".join(ids)

# ── HTML email ────────────────────────────────────────────────────────────────

STAGE_COLORS = {
    "Early": {"bg": "#FEF3E2", "border": "#E8A030", "dot": "#E8A030", "text": "#7A4A00", "subtext": "#E8A030"},
    "Mid":   {"bg": "#E6F1FB", "border": "#378ADD", "dot": "#378ADD", "text": "#0C447C", "subtext": "#378ADD"},
    "Late":  {"bg": "#EEFFF4", "border": "#27AE60", "dot": "#27AE60", "text": "#0A4A24", "subtext": "#27AE60"},
}

def build_html(data):
    total_roles     = data["total_roles"]
    total_headcount = data["total_headcount"]
    buckets         = data["buckets"]
    offers          = data["offer_acceptances"]
    new_roles       = data["new_roles"]
    generated_at    = data["generated_at"]

    headcount_label = (
        f"{total_roles} open role{'s' if total_roles != 1 else ''} for {total_headcount} headcount"
        if total_headcount != total_roles
        else f"{total_roles} open role{'s' if total_roles != 1 else ''}"
    )

    def stage_row(bucket):
        c = STAGE_COLORS[bucket]
        c_bg     = c["bg"]
        c_border = c["border"]
        c_dot    = c["dot"]
        c_text   = c["text"]
        c_sub    = c["subtext"]
        roles = buckets[bucket]
        count = len(roles)

        chips_html = ""
        if roles:
            for role in roles:
                stage_label = (
                    f"<div style='font-size:10px;color:{c_sub};margin-top:2px;'>{role['stage_name']}</div>"
                ) if role["stage_name"] else ""
                chips_html += (
                    f"<div style='display:inline-block;background:{c_bg};border:0.5px solid {c_border};"
                    f"border-radius:6px;padding:5px 9px;margin:0 6px 6px 0;vertical-align:top;'>"
                    f"<div style='font-size:12px;font-weight:600;color:{c_text};'>{role['name']}</div>"
                    f"<div style='font-size:11px;color:{c_sub};'>{role['ids_label']}</div>"
                    f"{stage_label}"
                    f"</div>"
                )
        else:
            chips_html = (
                f"<div style='display:inline-block;background:#f9f9f9;border:0.5px solid #e0e0e0;"
                f"border-radius:6px;padding:5px 9px;'>"
                f"<div style='font-size:12px;color:#aaa;font-style:italic;'>No roles</div>"
                f"</div>"
            )

        return (
            f"<tr style='border-bottom:1px solid #eee;'>"
            f"<td style='padding:14px 22px;'>"
            f"<div style='display:inline-block;margin-bottom:9px;'>"
            f"<span style='display:inline-block;width:8px;height:8px;border-radius:50%;"
            f"background:{c_dot};vertical-align:middle;margin-right:4px;'></span>"
            f"<span style='font-size:11px;font-weight:600;color:#444;vertical-align:middle;'>{bucket}</span>"
            f"<span style='font-size:11px;font-weight:600;color:{c_dot};vertical-align:middle;margin-left:3px;'>{count}</span>"
            f"</div>"
            f"<div>{chips_html}</div>"
            f"</td></tr>"
        )

    stages_html = stage_row("Early") + stage_row("Mid") + stage_row("Late")

    # Offer acceptances
    offers_html = ""
    if offers:
        offer_items = ""
        for o in offers:
            offer_items += (
                f"<div style='background:#EEFFF4;border:0.5px solid #27AE60;border-radius:6px;"
                f"padding:8px 12px;margin-bottom:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#0A4A24;'>{o['name']} accepted "
                f"&middot; {o['role']} <span style='font-weight:400;color:#27AE60;'>{o['job_id']}</span></div>"
                f"<div style='font-size:11px;color:#27AE60;margin-top:2px;'>Starts {o['start_date']}</div>"
                f"</div>"
            )
        label = "Offer Acceptances" if len(offers) > 1 else "Offer Acceptance"
        offers_html = (
            f"<tr><td colspan='2' style='padding:14px 22px 6px;'>"
            f"<div style='font-size:11px;font-weight:600;color:#27AE60;letter-spacing:0.04em;"
            f"margin-bottom:8px;'>OFFER ACCEPTANCE{'S' if len(offers)>1 else ''}</div>"
            f"{offer_items}"
            f"</td></tr>"
        )

    # New roles
    new_roles_html = ""
    if new_roles:
        nr_items = ""
        for r in new_roles:
            openings_note = f" &middot; {r['openings']} openings" if r['openings'] > 1 else ""
            nr_items += (
                f"<div style='background:#EEF4FF;border:0.5px solid #378ADD;border-radius:6px;"
                f"padding:8px 12px;margin-bottom:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#0C447C;'>{r['name']} "
                f"<span style='font-weight:400;color:#378ADD;'>{r['ids_label']}{openings_note}</span></div>"
                f"<div style='font-size:11px;color:#378ADD;margin-top:2px;'>Posted this week</div>"
                f"</div>"
            )
        new_roles_html = (
            f"<tr><td colspan='2' style='padding:14px 22px 6px;'>"
            f"<div style='font-size:11px;font-weight:600;color:#378ADD;letter-spacing:0.04em;"
            f"margin-bottom:8px;'>NEW ROLE{'S' if len(new_roles)>1 else ''} THIS WEEK</div>"
            f"{nr_items}"
            f"</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:24px 0;">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;">
  <tr><td style="background:#1A2E5A;padding:18px 22px;">
    <div style="color:#fff;font-size:17px;font-weight:600;">Pipeline Snapshot</div>
    <div style="color:#9BB3D4;font-size:12px;margin-top:3px;">{generated_at}</div>
  </td></tr>
  <tr><td style="background:#F0F4FF;padding:10px 22px;border-bottom:1px solid #E0E6F0;">
    <span style="font-size:14px;color:#1A2E5A;font-weight:600;">We currently have <b>{headcount_label}</b></span>
  </td></tr>
  <tr><td style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0">
      {stages_html}
      {offers_html}
      {new_roles_html}
    </table>
  </td></tr>
  <tr><td style="background:#f9f9f9;border-top:1px solid #eee;padding:9px 22px;text-align:center;">
    <span style="font-size:10px;color:#bbb;">Auto-generated from Greenhouse &bull; Code.org Talent Acquisition</span>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


# ── Gmail send ────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def create_draft(html_body):
    """Save the report as a Gmail draft for your review before sending."""
    today = datetime.date.today().strftime("%B %d, %Y")
    subject = f"Weekly Recruiting & Hiring Update - {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = "exec@code.org"
    msg ["Cc"] = "headcount@code.org"
    msg.attach(MIMEText(html_body, "html"))

    service = get_gmail_service()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    draft_id = draft.get("id", "")
    print(f"Draft created. Open Gmail and look for '{subject}' in your Drafts folder.")
    print(f"Draft ID: {draft_id}")
    return draft_id

def send_slack_notification(data):
    """Ping your Slack with a summary and a nudge to review the Gmail draft."""
    if not SLACK_WEBHOOK_URL:
        print("No SLACK_WEBHOOK_URL set — skipping Slack notification.")
        return

    buckets = data["buckets"]
    counts  = {k: len(v) for k, v in buckets.items()}
    total   = data["total_roles"]
    hc      = data["total_headcount"]

    offers_line = ""
    if data["offer_acceptances"]:
        names = ", ".join(o["name"] for o in data["offer_acceptances"])
        offers_line = f"\n🎉 *Offer acceptance{'s' if len(data['offer_acceptances']) > 1 else ''}:* {names}"

    new_roles_line = ""
    if data["new_roles"]:
        names = ", ".join(r["name"] for r in data["new_roles"])
        new_roles_line = f"\n🆕 *New role{'s' if len(data['new_roles']) > 1 else ''}:* {names}"

    message = {
        "text": "📋 Weekly Recruiting Snapshot — draft ready for review",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📋 Weekly Recruiting Snapshot"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{total} open role{'s' if total != 1 else ''} "
                        f"for {hc} headcount*\n"
                        f"🟦 Early: {counts['Early']}   "
                        f"🟧 Mid: {counts['Mid']}   "
                        f"🟩 Late: {counts['Late']}"
                        f"{offers_line}{new_roles_line}"
                    )
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "The full report is waiting in your Gmail Drafts. Review and send when ready. ✉️"
                }
            }
        ]
    }

    response = requests.post(SLACK_WEBHOOK_URL, json=message)
    if response.status_code == 200:
        print("Slack notification sent.")
    else:
        print(f"Slack notification failed: {response.status_code} {response.text}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Pulling data from Greenhouse...")
    data = build_report()
    print(f"Found {data['total_roles']} open roles across {data['total_headcount']} headcount.")
    html = build_html(data)

    # Write a local preview so you can check it before sending
    with open("report_preview.html", "w") as f:
        f.write(html)
    print("Preview saved to report_preview.html")

    create_draft(html)
    send_slack_notification(data)
