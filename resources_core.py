"""Shared Supabase resources table helpers for PDF and video uploads."""

import re
from pathlib import Path

from supabase import Client

from cli_prompts import prompt_input

ALLOWED_TOPICS = {"dsa-pathways", "timelines-deadlines", "interview-preparation"}

COURSE_1 = {
    "bucket": "resources-public",
    "path_prefix": "course-1/pdf/",
    "is_paid": False,
    "category": "course-1",
    "topics": {"dsa-pathways", "timelines-deadlines"},
}

COURSE_2 = {
    "bucket": "resources-paid",
    "path_prefix": "course-2/pdf/",
    "is_paid": True,
    "category": "course-2",
    "topics": {"interview-preparation"},
}

COURSES = {"1": COURSE_1, "2": COURSE_2}


def humanize_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = stem.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", stem).strip()


def validate_topic_for_course(topic: str, course: dict) -> None:
    if topic not in ALLOWED_TOPICS:
        raise ValueError(
            f"topic must be one of {sorted(ALLOWED_TOPICS)}, got {topic!r}"
        )
    if topic not in course["topics"]:
        raise ValueError(
            f"topic {topic!r} is not valid for {course['category']} "
            f"(allowed: {sorted(course['topics'])})"
        )


def pick_course(course_arg: str | None = None, *, content_label: str = "files") -> dict:
    if course_arg:
        if course_arg not in COURSES:
            print(f"\n✗ Invalid --course {course_arg!r}. Use 1 or 2.")
            exit(1)
        return COURSES[course_arg]

    print(f"\nWhich course are these {content_label} for?")
    print("  1. Course 1 (free) — public")
    print("  2. Course 2 (paid)")
    choice = prompt_input("\nCourse (1/2): ", show_paste_hint=False)
    if choice not in COURSES:
        print("Invalid course.")
        exit(1)
    return COURSES[choice]


def pick_topic(
    course: dict,
    topic_arg: str | None = None,
    *,
    content_label: str = "files",
) -> str:
    if topic_arg:
        if topic_arg not in course["topics"]:
            print(
                f"\n✗ topic {topic_arg!r} is not valid for {course['category']}. "
                f"Allowed: {sorted(course['topics'])}"
            )
            exit(1)
        return topic_arg

    topics = sorted(course["topics"])
    print(f"\nTopic for {course['category']} {content_label}:")
    for i, t in enumerate(topics, 1):
        print(f"  {i}. {t}")
    idx = int(prompt_input("\nSelect topic number: ", show_paste_hint=False)) - 1
    if idx < 0 or idx >= len(topics):
        print("Invalid topic.")
        exit(1)
    return topics[idx]


# ── PDF resources ─────────────────────────────────────────────────────────────


def _course_for_pdf_definition(defn: dict) -> dict | None:
    bucket = defn.get("bucket")
    file_path = defn.get("file_path", "")
    is_paid = defn.get("is_paid")

    if bucket == COURSE_1["bucket"] and file_path.startswith(COURSE_1["path_prefix"]):
        if is_paid is False or is_paid == False:  # noqa: E712
            return COURSE_1
    if bucket == COURSE_2["bucket"] and file_path.startswith(COURSE_2["path_prefix"]):
        if is_paid is True or is_paid == True:  # noqa: E712
            return COURSE_2
    return None


def validate_pdf_definition(defn: dict) -> None:
    required = {"bucket", "file_path", "title", "topic", "is_paid"}
    missing = required - defn.keys()
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

    topic = defn["topic"]
    if topic not in ALLOWED_TOPICS:
        raise ValueError(
            f"topic must be one of {sorted(ALLOWED_TOPICS)}, got {topic!r}"
        )

    file_path = defn["file_path"]
    if file_path.startswith("/"):
        raise ValueError("file_path must not have a leading slash")
    if not file_path.lower().endswith(".pdf"):
        raise ValueError(f"file_path must end with .pdf, got {file_path!r}")

    course = _course_for_pdf_definition(defn)
    if course is None:
        raise ValueError(
            "bucket/file_path/is_paid must match Course 1 "
            "(resources-public, course-1/pdf/, is_paid=false) or Course 2 "
            "(resources-paid, course-2/pdf/, is_paid=true)"
        )

    validate_topic_for_course(topic, course)


def build_pdf_resource_row(defn: dict) -> dict:
    row = {
        "title": defn["title"],
        "type": "pdf",
        "topic": defn["topic"],
        "bucket": defn["bucket"],
        "file_path": defn["file_path"],
        "is_paid": defn["is_paid"],
    }
    for key in ("description", "sort_order", "duration", "category"):
        if key in defn and defn[key] is not None:
            row[key] = defn[key]
    return row


def upload_pdf_bytes(sb: Client, bucket: str, file_path: str, file_bytes: bytes) -> None:
    sb.storage.from_(bucket).upload(
        path=file_path,
        file=file_bytes,
        file_options={
            "content-type": "application/pdf",
            "upsert": "true",
        },
    )


def upsert_pdf_resource_row(sb: Client, row: dict) -> str:
    try:
        result = (
            sb.table("resources")
            .upsert(row, on_conflict="bucket,file_path")
            .execute()
        )
        if result.data:
            return result.data[0]["id"]
    except Exception:
        pass

    existing = (
        sb.table("resources")
        .select("id")
        .eq("bucket", row["bucket"])
        .eq("file_path", row["file_path"])
        .execute()
    )
    if existing.data:
        resource_id = existing.data[0]["id"]
        sb.table("resources").update(row).eq("id", resource_id).execute()
        return resource_id

    result = sb.table("resources").insert(row).execute()
    return result.data[0]["id"]


def process_pdf(sb: Client, file_bytes: bytes, definition: dict) -> dict:
    validate_pdf_definition(definition)
    upload_pdf_bytes(sb, definition["bucket"], definition["file_path"], file_bytes)
    resource_id = upsert_pdf_resource_row(sb, build_pdf_resource_row(definition))
    return {
        "status": "ok",
        "bucket": definition["bucket"],
        "file_path": definition["file_path"],
        "resource_id": resource_id,
        "title": definition["title"],
    }


# ── Video resources ───────────────────────────────────────────────────────────


def build_video_resource_row(defn: dict) -> dict:
    row = {
        "title": defn["title"],
        "type": "video",
        "topic": defn["topic"],
        "is_paid": defn["is_paid"],
        "category": defn.get("category"),
    }
    for key in ("description", "sort_order", "duration"):
        if key in defn and defn[key] is not None:
            row[key] = defn[key]
    return row


def insert_video_resource(
    sb: Client,
    row: dict,
    *,
    existing_id: str | None = None,
) -> str:
    """Insert a new video resource row, or update metadata on an existing id."""
    if existing_id:
        sb.table("resources").update(row).eq("id", existing_id).execute()
        return existing_id

    result = sb.table("resources").insert(row).execute()
    return result.data[0]["id"]


def update_video_mux_ids(
    sb: Client,
    resource_id: str,
    asset_id: str,
    playback_id: str,
    *,
    signed: bool,
) -> None:
    sb.table("resources").update(
        {
            "mux_asset_id": asset_id,
            "mux_playback_id": playback_id,
            "mux_playback_signed": signed,
        }
    ).eq("id", resource_id).execute()
