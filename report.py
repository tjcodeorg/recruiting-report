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
        "application review",
        "informational",
        "agency",
        "sourcing",
        "resume review",
        "phone screen",
    ],
    "mid": [
        "homework",
        "loop",
        "panel",
        "skills assessment",
        "technical",
        "hiring manager interview",
    ],
    "late": [
        "reference",
        "final interview",
        "offer",
        "background",
        "executive interview",
    ],
}

# ── Overrides (edit config.json each week before the script runs) ─────────────

def load_overrides():
    """Load manual stage overrides from config.json."""
    try:
        with open("config.json") as f:
            return json.load(f).get("overrides", {})
    except FileNotFoundError:
        return {}

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
    Given a list of applications, return the stage bucket of the most
    advanced candidate (Late > Mid > Early).
    """
    priority = {"Late": 3, "Mid": 2, "Early": 1}
    best = "Early"
    best_stage_name = ""
    for app in applications:
        current_stage = app.get("current_stage")
        if not current_stage:
            continue
        stage_name = current_stage.get("name", "")
        bucket = classify_stage(stage_name)
        if priority.get(bucket, 0) > priority.get(best, 0):
            best = bucket
            best_stage_name = stage_name
    return best, best_stage_name

# ── Build report data ─────────────────────────────────────────────────────────

def build_report():
    overrides = load_overrides()
    token = get_gh_token()
    jobs = get_open_jobs(token)

    # Total headcount = sum of number_of_openings across open jobs
    total_headcount = sum(j.get("number_of_openings", 1) for j in jobs)
    total_roles = len(jobs)

    buckets = {"Early": [], "Mid": [], "Late": []}

    for job in jobs:
        job_id   = job["id"]
        job_name = job["name"]
        openings = job.get("number_of_openings", 1)

        # Build the job ID label (e.g. "#324" or "#331 & #338" for multiple openings)
        job_ids_label = _format_job_ids(job)

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

        buckets[bucket].append({
            "name": job_name,
            "ids_label": job_ids_label,
            "stage_name": stage_name,
            "openings": openings,
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
    "Early": {"bg": "#EEF4FF", "border": "#93B4FF", "dot": "#4A6EF5"},
    "Mid":   {"bg": "#FFF8EE", "border": "#FFD093", "dot": "#F5A623"},
    "Late":  {"bg": "#EEFFF4", "border": "#93FFBD", "dot": "#27AE60"},
}

def build_html(data):
    total_roles     = data["total_roles"]
    total_headcount = data["total_headcount"]
    buckets         = data["buckets"]
    offers          = data["offer_acceptances"]
    new_roles       = data["new_roles"]
    generated_at    = data["generated_at"]

    headcount_label = (
        f"{total_roles} open role{'s' if total_roles != 1 else ''} "
        f"for {total_headcount} headcount"
        if total_headcount != total_roles
        else f"{total_roles} open role{'s' if total_roles != 1 else ''}"
    )

    # Build stage cards
    stage_cards_html = ""
    for bucket in ["Early", "Mid", "Late"]:
        roles = buckets[bucket]
        count = len(roles)
        colors = STAGE_COLORS[bucket]
        roles_html = ""
        for role in roles:
            stage_label = f"<span style='color:#666;font-size:13px;'>{role['stage_name']}</span>" if role["stage_name"] else ""
            roles_html += f"""
            <div style='padding:10px 0;border-bottom:1px solid #eee;'>
              <div style='font-weight:600;font-size:15px;color:#222;'>
                {role['name']}
                <span style='font-weight:400;color:#888;font-size:13px;'>{role['ids_label']}</span>
              </div>
              {stage_label}
            </div>"""

        if not roles_html:
            roles_html = "<div style='color:#aaa;font-size:13px;padding:8px 0;'>No roles in this stage</div>"

        stage_cards_html += f"""
        <div style='flex:1;min-width:220px;background:{colors['bg']};border:1.5px solid {colors['border']};
                    border-radius:10px;padding:18px 20px;margin:8px;'>
          <div style='display:flex;align-items:center;margin-bottom:12px;'>
            <span style='width:12px;height:12px;border-radius:50%;background:{colors['dot']};
                         display:inline-block;margin-right:8px;'></span>
            <span style='font-weight:700;font-size:16px;color:#333;'>{bucket} Stage</span>
            <span style='margin-left:auto;background:{colors['border']};color:#333;font-weight:700;
                         border-radius:20px;padding:2px 10px;font-size:13px;'>{count}</span>
          </div>
          {roles_html}
        </div>"""

    # Offer acceptances section
    offers_html = ""
    if offers:
        items = ""
        for o in offers:
            items += f"""
            <tr>
              <td style='padding:8px 12px;font-weight:600;'>{o['name']}</td>
              <td style='padding:8px 12px;'>{o['role']} <span style='color:#888;'>{o['job_id']}</span></td>
              <td style='padding:8px 12px;color:#27AE60;font-weight:600;'>Starts {o['start_date']}</td>
            </tr>"""
        offers_html = f"""
        <div style='margin-top:28px;'>
          <h3 style='font-size:16px;font-weight:700;color:#333;margin-bottom:10px;'>
            🎉 Offer Acceptance{'s' if len(offers) > 1 else ''}
          </h3>
          <table style='width:100%;border-collapse:collapse;background:#EEFFF4;border-radius:8px;overflow:hidden;'>
            <thead>
              <tr style='background:#93FFBD;'>
                <th style='padding:8px 12px;text-align:left;font-size:13px;color:#333;'>Candidate</th>
                <th style='padding:8px 12px;text-align:left;font-size:13px;color:#333;'>Role</th>
                <th style='padding:8px 12px;text-align:left;font-size:13px;color:#333;'>Start Date</th>
              </tr>
            </thead>
            <tbody>{items}</tbody>
          </table>
        </div>"""

    # New roles section
    new_roles_html = ""
    if new_roles:
        items = "".join(
            f"<li style='margin-bottom:4px;'><strong>{r['name']}</strong> "
            f"({r['ids_label']}"
            f"{', ' + str(r['openings']) + ' openings' if r['openings'] > 1 else ''})</li>"
            for r in new_roles
        )
        new_roles_html = f"""
        <div style='margin-top:28px;'>
          <h3 style='font-size:16px;font-weight:700;color:#333;margin-bottom:10px;'>🆕 New Role{'s' if len(new_roles) > 1 else ''} This Week</h3>
          <ul style='margin:0;padding-left:20px;color:#333;font-size:15px;line-height:1.7;'>{items}</ul>
        </div>"""

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style='margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;'>
  <div style='max-width:720px;margin:32px auto;background:#fff;border-radius:12px;
              box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;'>

    <!-- Header -->
    <div style='background:#1A2E5A;padding:24px 32px;'>
      <h1 style='margin:0;color:#fff;font-size:20px;font-weight:700;letter-spacing:0.3px;'>
        Weekly Recruiting Snapshot
      </h1>
      <p style='margin:4px 0 0;color:#9BB3D4;font-size:14px;'>{generated_at}</p>
    </div>

    <!-- Summary bar -->
    <div style='background:#F0F4FF;padding:16px 32px;border-bottom:1px solid #E0E6F0;'>
      <p style='margin:0;font-size:17px;color:#1A2E5A;font-weight:600;'>
        We currently have <strong>{headcount_label}</strong>
      </p>
    </div>

    <!-- Stage cards -->
    <div style='padding:24px 24px 8px;'>
      <div style='display:flex;flex-wrap:wrap;margin:-8px;'>
        {stage_cards_html}
      </div>
    </div>

    <!-- Offer acceptances + New roles -->
    <div style='padding:0 32px 32px;'>
      {offers_html}
      {new_roles_html}
    </div>

    <!-- Footer -->
    <div style='background:#f9f9f9;border-top:1px solid #eee;padding:14px 32px;text-align:center;'>
      <p style='margin:0;font-size:12px;color:#aaa;'>
        Auto-generated from Greenhouse &bull; Code.org Talent Acquisition
      </p>
    </div>

  </div>
</body>
</html>"""
    return html

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
    subject = f"Weekly Recruiting Snapshot — {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENTS)
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
