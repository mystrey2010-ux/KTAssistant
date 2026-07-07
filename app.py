"""
KT Vault – Knowledge Transfer Snippet Manager for IT Support Teams.

A self-contained Flask application that lets 2nd/3rd line support engineers
log, tag, search, and review KT snippets with grammar checking via LMStudio AI.
"""

import io
from docx import Document
import json
import os
from datetime import datetime, timezone

import re

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
    source_filename = db.Column(db.String(512), nullable=True)  # e.g. "teams_summary.txt"
    source_lines = db.Column(db.String(64), nullable=True)  # e.g. "~line 10-30"
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
            "source_filename": self.source_filename or "",
            "source_lines": self.source_lines or "",
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
            source_filename=data.get("source_filename"),
            source_lines=data.get("source_lines"),
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
        # Check whether new columns exist on pre-existing DBs
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

        if "source_filename" not in columns:
            conn.execute(
                sa_text(
                    "ALTER TABLE snippets ADD COLUMN source_filename TEXT"
                )
            )

        if "source_lines" not in columns:
            conn.execute(
                sa_text(
                    "ALTER TABLE snippets ADD COLUMN source_lines TEXT"
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


def _sanitize_text(text: str) -> str:
    """Remove characters that break JSON encoding or LMStudio parsing (null bytes, control chars)."""
    return "".join(ch for ch in text if ord(ch) >= 32 and ch not in "\x07\x0b")


def _call_lmstudio(prompt, system_prompt=None, max_tokens_override: int | None = None):
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
        "max_tokens": max_tokens_override or 4096,
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
    except requests.exceptions.HTTPError as exc:  # pragma: no cover – network errors
        status = exc.response.status_code if exc.response else "?"
        error_body = ""
        try:
            error_body = exc.response.text[:500] if exc.response else "(no body)"
        except Exception:
            pass
        app.logger.error(
            "LMStudio HTTP %s (model=%s): %s | Body: %s",
            status, LMSTUDIO_MODEL, exc, error_body,
        )
        raise RuntimeError(
            f"Review service unavailable. LMStudio returned HTTP {status}: {error_body}"
        ) from exc
    except Exception as exc:  # pragma: no cover – other errors (e.g. JSON parse)
        app.logger.error("LMStudio request failed (%s): %s", type(exc).__name__, exc)
        raise RuntimeError(f"Review service unavailable: {exc}") from exc


def _extract_json_from_response(text: str) -> str:
    """Extract valid JSON from a response that might contain conversational text."""
    import re
    
    text = text.strip()
    
    # Try direct parse first
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    
    # Look for JSON-like structures in the response
    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    matches = re.findall(json_pattern, text)
    
    if matches:
        # Try each match to find valid JSON
        for match in matches:
            try:
                json.loads(match)
                return match
            except json.JSONDecodeError:
                continue
    
    # If all else fails, return original (will raise error downstream)
    app.logger.warning("Could not extract JSON from response: %r", text[:200])
    return text


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


# ---------------------------------------------------------------------------
# Check for duplicates (AI-powered semantic dedup)
# ---------------------------------------------------------------------------

@app.route("/api/snippets/check-duplicates", methods=["POST"])
def check_duplicates():
    """Return groups of likely duplicate snippets using AI analysis."""
    q = request.args.get("q") or (request.json or {}).get("q", "")
    account_id = request.args.get("account_id") or (request.json or {}).get("account_id", "")

    base_q = Snippet.query
    if account_id:
        try:
            base_q = base_q.filter(Snippet.account_id == int(account_id))
        except ValueError:
            pass  # ignore malformed account_id filter

    snippets = base_q.order_by(Snippet.timestamp.desc()).all()

    if not snippets:
        return jsonify({"groups": [], "total_checked": 0})

    # Build prompt — truncate long bodies for token budget (~400 chars)
    entries = []
    for s in snippets:
        body_short = (s.body or "")[:400]
        title = s.title or "Untitled"
        entries.append(f"- Snippet {s.id} ({title}): {body_short}")

    prompt = f"""You are a duplicate detection expert. Analyze these saved snippets and identify TRUE semantic duplicates.

**Saved Snippets to analyze:**
{chr(10).join(entries)}

---

**What counts as a DUPLICATE:**
Two snippets are duplicates if they describe the SAME core situation, problem, or instruction — even if using completely different words.

Look for:
- Same event described differently ("delayed launch due to bugs" = "push back release because of technical blockers")
- Same issue stated with synonyms ("software crashes" = "application goes down")  
- Same procedure rephrased ("reset your password" = "change your login credentials")

**Examples:**
✓ DUPLICATE: "We need to delay the launch due to unexpected bugs" AND "The release is postponed because we hit unforeseen technical issues during integration"
  → Both mean: project delayed because of development problems
  
✓ DUPLICATE: "Update password every 30 days per policy" AND "Change login credentials monthly as required"  
  → Same instruction, different wording

✗ NOT duplicate: Different topics entirely
✗ NOT duplicate: Same topic but different details ("fix Python bug on Windows" vs "fix Python bug on Mac")

**Your task:**
Compare ALL snippets above. Group together only those that are TRUE semantic duplicates (same meaning/intent). Return valid JSON:

{{"groups": [
  {{
    "reason": "Why these are duplicates (1 line, e.g., 'Both describe project delay due to unforeseen issues')",
    "snippet_ids": [ID1, ID2, ...]
  }}
]}}

Rules:
- Only include groups with 2+ snippets
- Be thorough — don't miss obvious semantic duplicates
- Be strict — only group if they convey essentially the same information
- Omit unique snippets that have no duplicates"""

    # Remove f-string formatting artifacts and ensure clean JSON template
    prompt = prompt.replace("{{", "{").replace("}}", "}")

    try:
        response = _call_lmstudio(prompt, max_tokens_override=4096)
    except RuntimeError as exc:
        app.logger.error("check-duplicates AI failed: %s", exc)
        return jsonify({"error": str(exc)}), 502

    # Try to extract valid JSON from response (handle conversational wrapping)
    cleaned_response = _extract_json_from_response(response)
    
    try:
        data = json.loads(cleaned_response)
    except json.JSONDecodeError as exc:
        app.logger.warning("LMStudio returned invalid JSON: %r", cleaned_response[:300])
        raise RuntimeError(f"AI returned invalid JSON: {exc}") from exc

    groups = []
    seen_ids = set()
    for g in data.get("groups", []):
        ids = sorted([int(x) for x in g.get("snippet_ids", [])], reverse=True)  # newest first
        if any(i in seen_ids for i in ids):
            continue  # skip overlapping groups
        snippets_in_group = [s for s in snippets if s.id in ids]
        groups.append({
            "reason": g.get("reason", ""),
            "snippets": [{
                "id": s.id,
                "title": (s.title or "Untitled"),
                "body": (s.body or "")[:200],
                "account_name": getattr(s.account, "name", "Unknown") if s.account else "Unknown",
                "category": s.category,
                "created_at": s.timestamp.isoformat() if s.timestamp else "",
            } for s in snippets_in_group],
        })
        seen_ids.update(ids)

    return jsonify({
        "groups": groups,
        "total_checked": len(snippets),
        "unique_count": len(snippets) - sum(len(g["snippets"]) for g in groups),
    })



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


@app.route("/api/snippets/delete-batch", methods=["POST"])
def delete_snippets_batch():
    """Delete multiple snippets by IDs in a single transaction."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")

    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({"error": "No snippet IDs provided."}), 400

    deleted_count = 0
    errors = []
    for sid in ids:
        try:
            snippet = db.session.get(Snippet, int(sid))
            if snippet:
                db.session.delete(snippet)
                deleted_count += 1
            else:
                errors.append(f"Snippet {sid} not found.")
        except Exception as exc:
            app.logger.error("Failed to delete snippet %d during batch: %s", sid, exc)
            errors.append(f"Snippet {sid}: {str(exc)}")

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.error("Batch commit failed: %s", exc)
        return jsonify({"error": "Internal server error."}), 500

    result = {"deleted": deleted_count, "not_found": len(errors)}
    if errors:
        result["errors"] = errors
    return jsonify(result)


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
                md_lines.append(f"**Snippet ID:** #{snippet.id}")

                if snippet.tags:
                    tags = ", ".join(t.strip() for t in snippet.tags.split(",") if t.strip())
                    md_lines.append(f"**Tags:** {tags}")

                if snippet.source_filename or snippet.source_lines:
                    parts = []
                    if snippet.source_filename:
                        parts.append(f"Source: `{snippet.source_filename}`")
                    if snippet.source_lines:
                        parts.append(snippet.source_lines)
                    md_lines.append(" ".join(parts))

                ts_str = (
                    snippet.timestamp.strftime("%Y-%m-%d %H:%M UTC")
                    if snippet.timestamp
                    else "N/A"
                )
                md_lines.append(f"*Date:* {ts_str}")
                md_lines.append(snippet.body)

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
_MAX_SNIPPETS_PER_CHUNK = 500  # Upper bound on raw snippets processed per chunk (practically unlimited)
_EXTRACT_MAX_TOKENS = 4096  # Cap output tokens for extract (keeps response compact, avoids context budget waste)


def _recover_json(text: str):
    """Parse AI response, recovering from truncated JSON (missing closing braces/brackets)."""
    text = text.strip()

    # Strip optional markdown code-fence wrappers (e.g. ```json ... ```)
    text = re.sub(r'^```\w*\n?', '', text)
    text = re.sub(r'\n?```$', '', text).strip()

    if not text.startswith("{"):
        return None  # not a JSON object

    # Strategy 1: find the last point where bracket depth returned to zero
    bracket_depth = 0
    best_end = -1
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            bracket_depth += 1
        elif ch in "}]":
            bracket_depth -= 1
            if bracket_depth >= 0:
                best_end = i + 1

    if best_end > 0:
        try:
            return json.loads(text[:best_end])
        except json.JSONDecodeError:
            pass

    # Strategy 2: extract all complete snippet objects from the array.
    # When LMStudio truncates mid-array, individual items may still be valid JSON.
    # Track every matching { } pair at any nesting depth (inner objects too).
    if '"snippets"' in text and "[" in text:
        snippets = []
        brace_stack: list[int] = []  # stack of open-brace positions for each depth level
        for i, ch in enumerate(text):
            if ch == '"' or ch == '\\':
                continue
            if ch == "{":
                brace_stack.append(i)
            elif ch == "}":
                if brace_stack:
                    start = brace_stack.pop()
                    try:
                        obj = json.loads(text[start:i + 1])
                        if "title" in obj and "body" in obj:
                            snippets.append(obj)
                    except json.JSONDecodeError:
                        pass

        if snippets:
            return {"snippets": snippets}

    # Strategy 3: progressively trim trailing content to find valid JSON
    for cut in range(len(text) - 1, max(0, len(text) - 500), -1):
        candidate = text[:cut].rstrip()
        if not (candidate.endswith("}") or candidate.endswith("]")):
            continue
        stripped = candidate.rstrip(",").rstrip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            continue

    # Strategy 4: find any { ... } block that looks like a snippet and wrap it.
    # This handles cases where the model started writing snippets but got truncated
    # before closing even the first one (no '}' characters at all).
    brace_start = text.rfind("{")
    if brace_start > 0 and "title" in text[brace_start : min(brace_start + 300, len(text))]:
        # Try progressively extending from brace_start to find valid JSON
        for end in range(len(text), brace_start, -1):
            candidate = text[brace_start:end].rstrip().rstrip(",").rstrip()
            if not candidate:
                continue
            try:
                obj = json.loads(candidate)
                return {"snippets": [obj]}
            except json.JSONDecodeError:
                continue

    # Strategy 5 (best-effort): try to find a matching closing brace position even in malformed text.
    if "title" in text:
        brace_positions = [i for i, ch in enumerate(text) if ch == "{"]
        if brace_positions:
            start = brace_positions[-1]
            depth = 0
            found_close = -1
            in_str = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if esc: esc = False; continue
                if ch == "\\" and in_str: esc = True; continue
                if ch == '"': in_str = not in_str; continue
                if in_str: continue
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0: found_close = i + 1; break

            if found_close > start:
                try:
                    obj = json.loads(text[start:found_close])
                    return {"snippets": [obj]}
                except json.JSONDecodeError:
                    pass

            # No closing brace found — likely truncated mid-string. Try appending
            # a closing quote + brace to complete the last open string value.
            candidate = text[start:] + '"' + "}"
            try:
                obj = json.loads(candidate)
                return {"snippets": [obj]}
            except json.JSONDecodeError:
                pass

    return None  # could not recover


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[tuple[str, int, int]]:
    """Split text into overlapping chunks so no sentence boundary is lost mid-chunk."""
    if not text.strip():
        return []

    # Fast path: whole document fits in one chunk
    lines = text.split("\n")
    if len(lines) <= size and len(text) <= size:
        return [(text.strip(), 1, max(1, len(lines)))]

    chunks: list[tuple[str, int, int]] = []
    start_pos = 0
    newlines_before_start = text[:start_pos].count("\n")

    while start_pos < len(text):
        end_pos = min(start_pos + size, len(text))
        
        # Try to break at a line boundary for cleaner context (look back into overlap zone)
        if end_pos < len(text):
            cut = text.rfind("\n", end_pos - overlap, end_pos)
            if cut > start_pos:
                end_pos = cut + 1
        
        chunk_text = text[start_pos:end_pos]
        
        # Line numbers are 1-based. Count how many newlines precede this chunk's start
        # to determine the starting line number.
        newlines_before = text[:start_pos].count("\n")
        newlines_in_chunk = chunk_text.count("\n")
        first_line = newlines_before + 1
        last_line = max(first_line, first_line + newlines_in_chunk - 1)
        
        chunks.append((chunk_text.strip(), first_line, last_line))
        
        start_pos = end_pos

    return chunks


def _merge_tiny_chunks(chunks: list[tuple[str, int, int]], min_chars: int = 150) -> list[tuple[str, int, int]]:
    """Merge adjacent chunks that are too small to extract meaningfully."""
    if len(chunks) <= 1:
        return chunks

    merged: list[tuple[str, int, int]] = []
    i = 0
    while i < len(chunks):
        chunk_text, first_line, last_line = chunks[i]
        # If this is a tiny chunk and there's a next one, merge into the next
        if len(chunk_text.strip()) < min_chars and i + 1 < len(chunks):
            next_text, next_first, next_last = chunks[i + 1]
            combined_text = f"{chunk_text} | {next_text}".strip()
            # Use the earlier first_line so the range starts from where this chunk began
            merged.append((combined_text, first_line, max(last_line, next_last)))
            i += 2
            continue
        # If this is tiny but it's the last chunk, try merging into previous instead
        elif len(chunk_text.strip()) < min_chars and merged:
            prev_text, prev_first, prev_last = merged[-1]
            combined_text = f"{prev_text} | {chunk_text}".strip()
            # Extend the previous range to cover this tiny chunk's lines too
            merged[-1] = (combined_text, prev_first, max(prev_last, last_line))
            i += 1
            continue
        else:
            merged.append(chunks[i])
            i += 1

    return merged


@app.route("/api/extract", methods=["POST"])
def extract_snippets():
    """Upload a text file (e.g. Teams AI summary), chunk it, send to LMStudio per chunk, merge results."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    file = request.files["file"]
    # Allow common text formats even without proper content-type; docx handled separately below
    if file.filename == "" or (file.content_type and not file.content_type.startswith("text/") and not file.filename.lower().endswith('.docx')):
        allowed_exts = (".txt", ".md", ".log", ".csv")
        if not any(file.filename.lower().endswith(ext) for ext in allowed_exts):
            return jsonify({"error": "Upload a supported file (.txt, .md, .log, .csv, or .docx)."}), 400

    try:
        raw_bytes = file.read()
    except Exception as exc:
        return jsonify({"error": f"Could not read uploaded file: {exc}"}), 400

    # Extract plain text — docx files are unpacked paragraph-by-paragraph; everything else is treated as UTF-8 text.
    if file.filename.lower().endswith(".docx"):
        try:
            doc = Document(io.BytesIO(raw_bytes))
            # Sanitize paragraphs individually BEFORE joining — sanitize strips newlines,
            # so joining after sanitization preserves paragraph separators as real \n chars.
            sanitized_paragraphs = (_sanitize_text(p.text) for p in doc.paragraphs)
            content = "\n".join(pt.strip() for pt in sanitized_paragraphs if pt.strip())
        except Exception as exc:  # pragma: no cover – malformed .docx
            return jsonify({"error": f"Could not read Word document: {exc}"}), 400
    else:
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return jsonify({"error": f"Could not decode file as UTF-8: {exc}"}), 400

    if not content.strip():
        return jsonify({"error": "File is empty."}), 400

    chunks = _chunk_text(content)
    chunks = _merge_tiny_chunks(chunks)
    num_chunks = len(chunks)
    total_lines = content.count("\n") + 1 if content.strip() else 0
    app.logger.info(
        "Extracted %d chunk(s) from '%s' (%d chars, ~%d lines)",
        num_chunks, file.filename, len(content), total_lines,
    )

    # Process each chunk through LMStudio and collect snippets
    all_raw_snippets: list[dict] = []
    for i, chunk in enumerate(chunks):
        app.logger.info(
            "Processing chunk %d/%d (lines %d-%d, %d chars)",
            i + 1, num_chunks, chunk[1], chunk[2], len(chunk[0]),
        )
        try:
            response_text = _call_lmstudio(
                prompt=chunk[0],          # chunk is (text, first_line, last_line) – pass only the text
                system_prompt=_EXTRACT_SYSTEM_PROMPT,
                max_tokens_override=_EXTRACT_MAX_TOKENS,
            )
        except RuntimeError as exc:  # pragma: no cover – network errors
            return jsonify({"error": f"LMStudio failed on chunk {i + 1}: {exc}"}), 503

        data = _recover_json(response_text)
        if data is None:
            reason = "empty response" if not response_text.strip() else f"unparseable ({response_text[:80]})"
            app.logger.warning("Could not parse extract response for chunk %d (%s)", i, reason)
            continue
        extracted = data.get("snippets") or []
        if isinstance(extracted, list):
            # Tag each snippet with chunk index for source tracking later
            tagged = []
            for item in extracted:
                tagged_item = dict(item)  # shallow copy to avoid mutating original
                tagged_item["_chunk_idx"] = i  # internal field, stripped before saving
                tagged.append(tagged_item)
            all_raw_snippets.extend(tagged)
            app.logger.info(
                "Chunk %d/%d produced %d snippet(s)",
                i + 1, num_chunks, len(extracted),
            )

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
        # Attach source metadata using the chunk index that produced this snippet.
        # This line is NOT part of body text, so AI review never sees it.
        # It gets appended to the saved body on /api/extract/save-all after any review pass.
        chunk_idx = item.get("_chunk_idx", 0) if isinstance(item, dict) else 0
        source_line_start = chunks[chunk_idx][1] if chunks and 0 <= chunk_idx < len(chunks) else 0
        source_line_end = chunks[chunk_idx][2] if chunks and 0 <= chunk_idx < len(chunks) else 0
        snippet_data["source"] = f"{file.filename} · ~line {source_line_start} - {source_line_end}"
        # Also store separately so the frontend can display them independently.
        snippet_data["source_filename"] = file.filename
        snippet_data["source_lines"] = (
            f"~line {source_line_start} – {source_line_end}" if source_line_start else ""
        )

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

    # File was fully processed via chunking — no content lost
    return jsonify({
        "source": file.filename,
        "chunks_processed": num_chunks,
        "extracted_total": len(all_raw_snippets),
        "truncated": False,
        "snippets": results,  # show all extracted snippets for user selection
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

        # Extract source metadata if present (from Load File extraction)
        source_filename = item.get("source_filename")  # may be None for manually-created snippets
        source_lines = item.get("source_lines")  # may be None for manually-created snippets

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
            account_id=account_id, title=title, category=category, body=body, tags=tags,
            source_filename=source_filename or None,
            source_lines=source_lines or None,
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
