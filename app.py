"""
KT Vault – Knowledge Transfer Snippet Manager for IT Support Teams.

A self-contained Flask application that lets 2nd/3rd line support engineers
log, tag, search, and review KT snippets with grammar checking via LMStudio AI.
"""

import io
import json
import os
from datetime import datetime, timezone

import requests
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)
from flask_sqlalchemy import SQLAlchemy

# ---------------------------------------------------------------------------
# App & Database Setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "kt-vault-dev-key")
app.config[
    "SQLALCHEMY_DATABASE_URI"
] = f"sqlite:///{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kt_vault.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Database Model
# ---------------------------------------------------------------------------


class Account(db.Model):
    """A customer/account that KT snippets belong to."""

    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(256), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True, default="")
    timestamp = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self):
        """Serialize account to a JSON-friendly dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
        }


class Snippet(db.Model):
    """A single Knowledge Transfer snippet linked to an Account."""

    __tablename__ = "snippets"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    account_id = db.Column(
        db.Integer, db.ForeignKey("accounts.id"), nullable=False
    )
    title = db.Column(db.String(256), nullable=False)
    category = db.Column(db.String(100), nullable=False, default="General")
    body = db.Column(db.Text, nullable=False)
    tags = db.Column(db.String(512), nullable=True)  # comma-separated tag string
    timestamp = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    account = db.relationship("Account", backref=db.backref("snippets", lazy="dynamic"))

    @property
    def account_name(self):
        """Convenience accessor for the linked account's name."""
        return self.account.name if self.account else "Unknown"

    def to_dict(self):
        """Serialize snippet to a JSON-friendly dictionary."""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "title": self.title,
            "category": self.category,
            "body": self.body,
            "tags": self.tags or "",
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }

    @staticmethod
    def from_dict(data):
        """Create a Snippet instance from a dictionary (used by import)."""
        return Snippet(
            title=data.get("title", "").strip(),
            category=data.get("category", "General").strip(),
            body=data.get("body", "").strip(),
            tags=data.get("tags", ""),
        )


# ---------------------------------------------------------------------------
# Schema migration helper – adds account_id column to pre-existing DBs.
# ---------------------------------------------------------------------------


def _migrate_schema():
    """Ensure the SQLite schema matches current models (idempotent)."""
    from sqlalchemy import text as sa_text

    conn = db.engine.connect()
    trans = conn.begin()
    try:
        # Check whether snippets.account_id exists
        cursor = conn.execute(
            sa_text("PRAGMA table_info(snippets)")
        )
        columns = {row[1] for row in cursor}
        if "account_id" not in columns:
            conn.execute(
                sa_text(
                    "ALTER TABLE snippets ADD COLUMN account_id INTEGER "
                    "REFERENCES accounts(id)"
                )
            )
    except Exception as exc:  # pragma: no cover – depends on DB state
        app.logger.warning("Schema migration warning: %s", exc)
        trans.rollback()
    else:
        trans.commit()


# Create tables on first request (avoid creating them during imports)
with app.app_context():
    db.create_all()
    _migrate_schema()

# ---------------------------------------------------------------------------
# LMStudio / OpenAI-compatible API config
# ---------------------------------------------------------------------------

LMSTUDIO_ENDPOINT = os.environ.get("LMSTUDIO_ENDPOINT", "http://192.168.50.2:1234")
LMSTUDIO_MODEL = os.environ.get("LMSTUDIO_MODEL", "ornith-1.0-35b")


def _call_lmstudio(prompt, system_prompt=None):
    """Send a prompt to LMStudio (OpenAI-compatible) and return the text response."""
    url = f"{LMSTUDIO_ENDPOINT}/v1/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": LMSTUDIO_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    try:
        resp = requests.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.ConnectionError as exc:
        app.logger.error("LMStudio connection failed: %s", exc)
        raise RuntimeError(
            "Could not reach LMStudio. Check that it's running and the endpoint is correct."
        ) from exc
    except Exception as exc:  # pragma: no cover – network errors
        app.logger.error("LMStudio request failed: %s", exc)
        raise RuntimeError(f"Review service unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# Routes – Extract / Review / Enhance (AI-powered via LMStudio)
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = (
    "You are a senior IT support knowledge transfer editor. Given the text below, "
    "extract only the relevant snippets of information that would be useful for an "
    "IT support engineer to perform a task or know as essential context.\n\n"
    "Focus on:\n"
    '  - Actionable procedures and step-by-step instructions\n'
    '  - Known issues, workarounds, and fixes\n'
    '  - Configuration details (IP addresses, service names, account names)\n'
    '  - Important decisions or action items with assigned owners\n'
    '  - Error codes and their resolutions\n\n'
    "Ignore:\n"
    '  - General pleasantries, meeting logistics, attendance lists\n'
    '  - Redundant information already captured elsewhere\n'
    '  - Speculation or unconfirmed details\n\n'
    "Respond ONLY with valid JSON — no markdown fences, no explanations outside the JSON.\n\n"
    "Return an array of extracted snippets. Each snippet must have this structure:\n"
    '{\n'
    '  "snippets": [\n'
    '    {\n'
    '      "title": "...short descriptive title...",\n'
    '      "body": "...the extracted information, self-contained and clear...",\n'
    '      "category": "...appropriate category (e.g. Procedure, Known Issue, Configuration)...",\n'
    '      "tags": "...comma-separated keywords..."\n'
    '    }\n'
    '  ]\n'
    '}'
)


_REVIEW_SYSTEM_PROMPT = (
    "You are a senior IT support knowledge transfer editor. Review the provided snippet for two things:\n\n"

    "1. LANGUAGE — grammar, spelling, clarity, and style issues.\n"
    '   For each issue include: type ("grammar", "clarity", "style", or "spelling"), original text, corrected suggestion, and a brief reason.\n\n'

    "2. CONTENT — missing steps, incomplete procedures, unclear references (e.g. \"step 3\" without step 2), "
    "security gaps (e.g. running as admin without warning), best-practice gaps, or any other substance-level concern.\n"
    '   For each content gap include: type ("missing_step", "unclear_reference", "incomplete_flow", "security_gap", or "best_practice"), location hint, description of the gap, and severity ("low", "medium", or "high").\n\n'

    "Also provide actionable enhancement suggestions — concrete text the author can paste in to improve the snippet.\n"
    '   For each include: type (e.g. "best_practice"), context (where it applies), and the suggestion text itself.\n\n'

    "Respond ONLY with valid JSON — no markdown fences, no explanations outside the JSON.\n\n"
    "Return exactly this structure:\n"
    '{\n'
    '  "issues": [\n'
    '    {\n'
    '      "type": "...", "original": "...", "suggestion": "...", "reason": "..."\n'
    '    }\n'
    '  ],\n'
    '  "content_gaps": [\n'
    '    {\n'
    '      "type": "missing_step" | "unclear_reference" | "incomplete_flow" | "security_gap" | "best_practice",\n'
    '      "location": "...hint about where in the text...",\n'
    '      "description": "...what is missing or wrong...",\n'
    '      "severity": "low" | "medium" | "high"\n'
    '    }\n'
    '  ],\n'
    '  "enhancement_suggestions": [\n'
    '    {\n'
    '      "type": "...", "context": "...where to apply...", "suggestion": "...text to add..."\n'
    '    }\n'
    '  ],\n'
    '  "corrected_body": "...the full text with all language corrections applied..." (keep content additions as gaps, not edits)\n'
    '}'
)


@app.route("/review", methods=["POST"])
def review_snippet():
    """Analyze snippet text using LMStudio AI and return structured issues + corrected version."""
    payload = request.get_json(silent=True) or {}
    text = (payload.get("body") or "").strip()

    if not text:
        return jsonify({"error": "No text provided for review."}), 400

    # Cap input length to keep the prompt reasonable
    max_input = 4096
    truncated = len(text) > max_input
    review_text = (text[:max_input] + "\n\n[... text truncated ...]") if truncated else text

    try:
        response_text = _call_lmstudio(
            prompt=review_text,
            system_prompt=_REVIEW_SYSTEM_PROMPT,
        )
    except RuntimeError as exc:  # pragma: no cover – network errors
        return jsonify({"error": str(exc)}), 503

    # Parse AI response (it should be valid JSON)
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        app.logger.warning("LMStudio returned non-JSON: %s", response_text[:200])
        return jsonify({"error": "Review service returned an unexpected response.", "raw_response": response_text}), 502

    # Validate expected keys
    issues = data.get("issues") or []
    corrected_body = data.get("corrected_body", text)

    errors = []
    for issue in issues[:20]:  # Cap at 20 to keep response manageable
        errors.append(
            {
                "type": issue.get("type", "grammar"),
                "original": str(issue.get("original", "")),
                "suggestion": str(issue.get("suggestion", "")),
                "reason": str(issue.get("reason", "")),
            }
        )

    # Parse content gaps (new in v2)
    content_gaps = []
    for gap in data.get("content_gaps") or []:
        content_gaps.append(
            {
                "type": gap.get("type", "best_practice"),
                "location": str(gap.get("location", "")),
                "description": str(gap.get("description", "")),
                "severity": gap.get("severity", "low"),
            }
        )

    # Parse enhancement suggestions (new in v2)
    enhancement_suggestions = []
    for enh in data.get("enhancement_suggestions") or []:
        enhancement_suggestions.append(
            {
                "type": str(enh.get("type", "")),
                "context": str(enh.get("context", "")),
                "suggestion": str(enh.get("suggestion", "")),
            }
        )

    return jsonify({
        "issues": errors,
        "content_gaps": content_gaps,
        "enhancement_suggestions": enhancement_suggestions,
        "corrected_body": corrected_body,
        "truncated": truncated,
    })


# ---------------------------------------------------------------------------
# Routes – Page & CRUD
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Render the main dashboard page."""
    return render_template("index.html")


@app.route("/api/snippets", methods=["GET"])
def list_snippets():
    """Return all snippets as JSON, optionally filtered by ?q= and/or ?account_id=."""
    query = request.args.get("q", "").strip().lower()
    account_id = request.args.get("account_id", "").strip()

    base_q = Snippet.query
    if account_id:
        try:
            base_q = base_q.filter(Snippet.account_id == int(account_id))
        except ValueError:
            pass  # ignore malformed account_id filter
    if not query:
        snippets = base_q.order_by(Snippet.timestamp.desc()).all()
    else:
        snippets = (
            base_q.filter(
                db.or_(
                    Snippet.title.ilike(f"%{query}%"),
                    Snippet.category.ilike(f"%{query}%"),
                    Snippet.body.ilike(f"%{query}%"),
                    Snippet.tags.ilike(f"%{query}%"),
                )
            )
            .order_by(Snippet.timestamp.desc())
            .all()
        )
    return jsonify([s.to_dict() for s in snippets])


@app.route("/api/snippets", methods=["POST"])
def create_snippet():
    """Create a new snippet from form data. `account_id` is required."""
    try:
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "General").strip()
        body = request.form.get("body", "").strip()
        tags = request.form.get("tags", "").strip()
        account_id_str = request.form.get("account_id", "").strip()

        if not title or not body:
            return jsonify({"error": "Title and body are required."}), 400
        if not account_id_str:
            return jsonify({"error": "An Account must be selected for every snippet."}), 400

        try:
            account_id = int(account_id_str)
        except ValueError:
            return jsonify({"error": "Invalid Account ID."}), 400

        account = db.session.get(Account, account_id)
        if not account:
            return jsonify({"error": f"Account #{account_id} does not exist."}), 404

        snippet = Snippet(
            title=title, category=category, body=body, tags=tags, account_id=account_id
        )
        db.session.add(snippet)
        db.session.commit()
        return jsonify({"message": "Snippet saved.", "snippet": snippet.to_dict()}), 201

    except Exception as exc:
        db.session.rollback()
        app.logger.error("Failed to create snippet: %s", exc)
        return jsonify({"error": "Internal server error."}), 500


@app.route("/api/snippets/<int:snippet_id>", methods=["DELETE"])
def delete_snippet(snippet_id):
    """Delete a snippet by ID."""
    snippet = db.session.get(Snippet, snippet_id)
    if not snippet:
        return jsonify({"error": "Snippet not found."}), 404

    try:
        account_name = snippet.account_name
        db.session.delete(snippet)
        db.session.commit()
        return jsonify({
            "message": f"Snippet {snippet_id} deleted.",
            "account_name": account_name
        })

    except Exception as exc:
        db.session.rollback()
        app.logger.error("Failed to delete snippet %d: %s", snippet_id, exc)
        return jsonify({"error": "Internal server error."}), 500


@app.route("/api/snippets/<int:snippet_id>", methods=["PUT"])
def update_snippet(snippet_id):
    """Update an existing snippet."""
    snippet = db.session.get(Snippet, snippet_id)
    if not snippet:
        return jsonify({"error": "Snippet not found."}), 404

    try:
        data = request.get_json(silent=True) or {}
        title = data.get("title", "").strip()
        category = data.get("category", "General").strip()
        body = data.get("body", "").strip()
        tags = data.get("tags", "").strip()
        account_id_str = data.get("account_id", "").strip()

        if not title or not body:
            return jsonify({"error": "Title and body are required."}), 400
        if not account_id_str:
            return jsonify({"error": "An Account must be selected for every snippet."}), 400

        try:
            account_id = int(account_id_str)
        except ValueError:
            return jsonify({"error": "Invalid Account ID."}), 400

        account = db.session.get(Account, account_id)
        if not account:
            return jsonify({"error": f"Account #{account_id} does not exist."}), 404

        snippet.title = title
        snippet.category = category
        snippet.body = body
        snippet.tags = tags
        snippet.account_id = account_id
        db.session.commit()

        return jsonify({
            "message": f"Snippet {snippet_id} updated.",
            "snippet": snippet.to_dict()
        })

    except Exception as exc:
        db.session.rollback()
        app.logger.error("Failed to update snippet %d: %s", snippet_id, exc)
        return jsonify({"error": "Internal server error."}), 500


# ---------------------------------------------------------------------------
# Routes – Account CRUD
# ---------------------------------------------------------------------------


@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    """Return all accounts as a JSON array."""
    accounts = Account.query.order_by(Account.name).all()
    return jsonify([a.to_dict() for a in accounts])


@app.route("/api/accounts", methods=["POST"])
def create_account():
    """Create a new account from form data."""
    try:
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            return jsonify({"error": "Account name is required."}), 400

        # Reject duplicates (unique constraint)
        existing = Account.query.filter(
            db.func.lower(Account.name) == name.lower()
        ).first()
        if existing:
            return jsonify({
                "error": f"Account '{name}' already exists.",
                "id": existing.id,
            }), 409

        account = Account(name=name, description=description or "")
        db.session.add(account)
        db.session.commit()
        return jsonify({"message": "Account created.", "account": account.to_dict()}), 201

    except Exception as exc:
        db.session.rollback()
        app.logger.error("Failed to create account: %s", exc)
        return jsonify({"error": "Internal server error."}), 500


@app.route("/api/accounts/<int:account_id>", methods=["PUT"])
def update_account(account_id):
    """Update an existing account's name or description."""
    account = db.session.get(Account, account_id)
    if not account:
        return jsonify({"error": "Account not found."}), 404

    try:
        data = request.get_json(silent=True) or {}
        new_name = (data.get("name") or "").strip()
        new_desc = (data.get("description") or "").strip()

        if new_name:
            # Check uniqueness (case-insensitive, excluding self)
            dup = Account.query.filter(
                db.and_(db.func.lower(Account.name) == new_name.lower(), Account.id != account_id)
            ).first()
            if dup:
                return jsonify({"error": f"Account '{new_name}' already exists."}), 409
            account.name = new_name

        account.description = new_desc
        db.session.commit()
        return jsonify({"message": "Account updated.", "account": account.to_dict()})

    except Exception as exc:
        db.session.rollback()
        app.logger.error("Failed to update account %d: %s", account_id, exc)
        return jsonify({"error": "Internal server error."}), 500


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    """Delete an account. Refuses if it still owns snippets (409)."""
    account = db.session.get(Account, account_id)
    if not account:
        return jsonify({"error": "Account not found."}), 404

    snippet_count = Snippet.query.filter_by(account_id=account_id).count()
    if snippet_count > 0:
        return jsonify({
            "error": f"Cannot delete: {snippet_count} snippet(s) still linked to this account. Unlink them first.",
            "snippet_count": snippet_count,
        }), 409

    try:
        db.session.delete(account)
        db.session.commit()
        return jsonify({"message": f"Account '{account.name}' deleted."})
    except Exception as exc:
        db.session.rollback()
        app.logger.error("Failed to delete account %d: %s", account_id, exc)
        return jsonify({"error": "Internal server error."}), 500


# ---------------------------------------------------------------------------
# Routes – Export / Import (JSON backup & mobility)
# ---------------------------------------------------------------------------


@app.route("/api/export")
def export_snippets():
    """Export all snippets as a downloadable JSON file."""
    snippets = Snippet.query.order_by(Snippet.timestamp.desc()).all()
    data = [s.to_dict() for s in snippets]

    output = io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))
    return send_file(
        output,
        mimetype="application/json",
        as_attachment=True,
        download_name="kt_vault_export.json",
    )


@app.route("/api/export/markdown")
def export_markdown():
    """Export snippets for a specific account as a downloadable Markdown report file."""
    account_id_str = request.args.get("account_id", "").strip()

    if not account_id_str:
        return jsonify({"error": "No account specified."}), 400

    try:
        account_id_int = int(account_id_str)
    except ValueError:
        return jsonify({"error": "Invalid account ID."}), 400

    account = db.session.get(Account, account_id_int)
    if not account:
        return jsonify({"error": f"Account #{account_id_int} does not exist."}), 404

    snippets = (
        Snippet.query.filter_by(account_id=account_id_int)
        .order_by(Snippet.timestamp.desc())
        .all()
    )

    # Build Markdown content
    md_lines: list[str] = []
    md_lines.append(f"# KT Report: {account.name}")
    md_lines.append("")
    generated_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md_lines.append(f"_Generated on {generated_ts}_")

    if account.description:
        md_lines.append("")
        md_lines.append(f"**Description:** {account.description}")

    md_lines.append("")
    md_lines.append("## Summary")
    md_lines.append("")
    md_lines.append(f"- **Total snippets:** {len(snippets)}")

    # Category breakdown
    if snippets:
        category_counts: dict[str, int] = {}
        for s in snippets:
            category_counts[s.category] = category_counts.get(s.category, 0) + 1
        parts = [f"{cat} ({count})" for cat, count in sorted(category_counts.items())]
        md_lines.append(f"- **Categories:** {', '.join(parts)}")

    md_lines.append("")

    if not snippets:
        md_lines.append("*No snippets found for this account.*")
    else:
        # Group by category
        grouped: dict[str, list[Snippet]] = {}
        for s in snippets:
            grouped.setdefault(s.category, []).append(s)

        for category, cat_snippets in sorted(grouped.items()):
            md_lines.append(f"## Category: {category}")
            md_lines.append("")

            for snippet in cat_snippets:
                md_lines.append(f"### {snippet.title}")
                md_lines.append("")

                if snippet.tags:
                    tags = ", ".join(t.strip() for t in snippet.tags.split(",") if t.strip())
                    md_lines.append(f"**Tags:** {tags}")
                    md_lines.append("")

                ts_str = (
                    snippet.timestamp.strftime("%Y-%m-%d %H:%M UTC")
                    if snippet.timestamp
                    else "N/A"
                )
                md_lines.append(f"*Date:* {ts_str}")
                md_lines.append("")
                md_lines.append("```text")
                md_lines.append(snippet.body)
                md_lines.append("```")
                md_lines.append("")

    content = "\n".join(md_lines)
    safe_name = account.name.replace(" ", "_").replace("/", "_")
    filename = f"kt_report_{safe_name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"

    output = io.BytesIO(content.encode("utf-8"))
    return send_file(
        output,
        mimetype="text/markdown",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/import", methods=["POST"])
def import_snippets():
    """Import snippets from an uploaded JSON file. Merges by title+category."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    file = request.files["file"]
    if file.filename == "" or not file.filename.endswith(".json"):
        return jsonify({"error": "Upload a .json file."}), 400

    try:
        raw = file.read().decode("utf-8")
        imported = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return jsonify({"error": f"Invalid JSON file: {exc}"}), 400

    if not isinstance(imported, list):
        return jsonify({"error": "JSON root must be an array of snippets."}), 400

    imported_titles = set()
    for item in imported:
        title = str(item.get("title", "")).strip()
        category = str(item.get("category", "General")).strip()
        if not title or not item.get("body"):
            continue
        key = (title.lower(), category.lower())

        # Check for duplicates by title+category
        existing = Snippet.query.filter(
            db.and_(Snippet.title.ilike(title), Snippet.category.ilike(category))
        ).first()

        if existing:
            # Update in place instead of duplicating
            existing.body = str(item["body"]).strip()
            if "tags" in item and item["tags"]:
                existing.tags = str(item["tags"])
        else:
            snippet = Snippet.from_dict(item)
            db.session.add(snippet)

        imported_titles.add(key)

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        db.session.rollback()
        app.logger.error("Import failed: %s", exc)
        return jsonify({"error": "Failed to save imports."}), 500

    count = len(imported_titles)
    return jsonify({"message": f"Imported {count} snippet(s)."}), 201


# ---------------------------------------------------------------------------
# Routes – Extract relevant snippets from uploaded text file (AI-powered)
# ---------------------------------------------------------------------------


_CHUNK_SIZE = 4096          # Characters per chunk sent to LMStudio
_CHUNK_OVERLAP = 256        # Overlap between chunks to preserve context continuity
_MAX_SNIPPETS_PER_CHUNK = 10


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks so no sentence boundary is lost mid-chunk."""
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        # Try to break at a sentence boundary for cleaner context
        if end < len(text):
            # Look backwards up to `overlap` chars for the nearest line break
            cut = text.rfind("\n", end - overlap, end)
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end].strip())
        start = end - overlap if end < len(text) else end

    return [c for c in chunks if c]


@app.route("/api/extract", methods=["POST"])
def extract_snippets():
    """Upload a text file (e.g. Teams AI summary), chunk it, send to LMStudio per chunk, merge results."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    file = request.files["file"]
    if file.filename == "" or file.content_type and not file.content_type.startswith("text/"):
        # Allow common text formats even without proper content-type
        allowed_exts = (".txt", ".md", ".log", ".csv")
        if not any(file.filename.lower().endswith(ext) for ext in allowed_exts):
            return jsonify({"error": "Upload a text-based file (.txt, .md, .log)."}), 400

    try:
        raw = file.read()
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return jsonify({"error": f"Could not decode file as UTF-8: {exc}"}), 400

    if not content.strip():
        return jsonify({"error": "File is empty."}), 400

    chunks = _chunk_text(content)
    num_chunks = len(chunks)
    app.logger.info("Extracted %d chunk(s) from '%s' (%d chars total)", num_chunks, file.filename, len(content))

    # Process each chunk through LMStudio and collect snippets
    all_raw_snippets: list[dict] = []
    for i, chunk in enumerate(chunks):
        try:
            response_text = _call_lmstudio(
                prompt=chunk,
                system_prompt=_EXTRACT_SYSTEM_PROMPT,
            )
        except RuntimeError as exc:  # pragma: no cover – network errors
            return jsonify({"error": f"LMStudio failed on chunk {i + 1}: {exc}"}), 503

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            app.logger.warning("LMStudio returned non-JSON for extract chunk %d: %s", i, response_text[:200])
            continue

        extracted = data.get("snippets") or []
        if isinstance(extracted, list):
            all_raw_snippets.extend(extracted)

    # Deduplicate and normalise snippets (by title — most chunks won't produce duplicates across chunk boundaries)
    seen_titles: set[str] = set()
    results: list[dict] = []
    for item in all_raw_snippets[:_MAX_SNIPPETS_PER_CHUNK * num_chunks]:  # generous upper bound before dedup
        snippet_data = {
            "title": str(item.get("title", "")).strip(),
            "body": str(item.get("body", "")).strip(),
            "category": str(item.get("category", "General")).strip() or "General",
            "tags": str(item.get("tags", "")).strip(),
        }
        if not snippet_data["title"] and not snippet_data["body"]:
            continue
        # Validate body has substance (at least 10 chars to filter noise)
        if len(snippet_data["body"]) < 10:
            continue

        dedup_key = snippet_data["title"].lower()
        if dedup_key in seen_titles:
            continue  # skip duplicate title
        seen_titles.add(dedup_key)
        results.append(snippet_data)

    truncated = len(content) > _CHUNK_SIZE
    return jsonify({
        "source": file.filename,
        "chunks_processed": num_chunks,
        "truncated": truncated,
        "snippets": results[:_MAX_SNIPPETS_PER_CHUNK],  # final cap at 10
    })


@app.route("/api/extract/save-all", methods=["POST"])
def save_all_extracted():
    """Bulk-save extracted snippets to the vault for a given account. Duplicates (same title+category) are skipped."""
    data = request.get_json(silent=True) or {}

    account_id_str = str(data.get("account_id", "")).strip()
    if not account_id_str:
        return jsonify({"error": "No account selected."}), 400

    try:
        account_id = int(account_id_str)
    except ValueError:
        return jsonify({"error": "Invalid account ID."}), 400

    account = db.session.get(Account, account_id)
    if not account:
        return jsonify({"error": f"Account #{account_id} does not exist."}), 404

    snippets_in = data.get("snippets") or []
    if not isinstance(snippets_in, list):
        return jsonify({"error": "Expected a 'snippets' array in the request body."}), 400

    saved_count = 0
    skipped_count = 0
    errors: list[str] = []

    for item in snippets_in:
        title = str(item.get("title", "")).strip()
        category = str(item.get("category", "General")).strip() or "General"
        body = str(item.get("body", "")).strip()
        tags = str(item.get("tags", "")).strip()

        if not title or len(body) < 10:
            skipped_count += 1
            continue

        # Skip duplicates by title+category (case-insensitive)
        existing = Snippet.query.filter(
            db.and_(Snippet.title.ilike(title), Snippet.category.ilike(category))
        ).first()
        if existing:
            skipped_count += 1
            continue

        snippet = Snippet(
            account_id=account_id, title=title, category=category, body=body, tags=tags
        )
        db.session.add(snippet)
        saved_count += 1

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        db.session.rollback()
        app.logger.error("save_all_extracted failed: %s", exc)
        return jsonify({"error": "Failed to save snippets."}), 500

    return jsonify({
        "saved": saved_count,
        "skipped_duplicates": skipped_count,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
