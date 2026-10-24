"""Daily digest facts: counts for one capture day, rendered as Markdown before any model sees them."""
from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path


def parse_day(text) -> date:
    """Canonical YYYY-MM-DD only; `date.fromisoformat` alone would also accept 20261022 or week dates."""
    try:
        day = date.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        day = None
    if day is None or day.isoformat() != text:
        raise ValueError("date must be YYYY-MM-DD")
    return day


def day_facts(library, day: date) -> dict:
    """Facts over photos whose stored `captured_at` date equals `day`; astro jobs count by their creation date."""
    iso = day.isoformat()
    keepers = set(library.keeper_ids())
    photos, bursts = [], set()
    for group in library.list_groups():
        for photo in group["photos"]:
            if (photo["captured_at"] or "")[:10] == iso:
                photos.append(photo | {"burst": group["id"]})
                bursts.add(group["id"])
    keywords, places = Counter(), Counter()
    for photo in photos:
        place = library.catalog.get_photo(photo["id"]).place_label
        if place:
            places[place] += 1
        tags = library.catalog.get_tags(photo["id"])
        subjects = (tags or {}).get("data", {}).get("subjects") or []
        keywords.update(subject for subject in set(subjects) if isinstance(subject, str))
    kept = [photo for photo in photos if photo["decision"] != "reject"]
    top = sorted(kept, key=lambda photo: (-(photo["total_score"] if photo["total_score"] is not None else -1), photo["path"]))[:5]
    astro = [library.job_status(job.id) for job in library.catalog.list_jobs()
             if job.kind == "astro" and job.created_at.astimezone().date() == day]
    return {
        "date": iso,
        "frames": len(photos),
        "keepers": sum(photo["id"] in keepers for photo in photos),
        "rejects": len(photos) - len(kept),
        "bursts": len(bursts),
        "astro_jobs": [{"job_id": job["job_id"], "status": job["status"], "lights": job["lights"]} for job in astro],
        "places": sorted(places.items(), key=lambda item: (-item[1], item[0])),
        "keywords": [[name, count] for name, count in sorted(keywords.items(), key=lambda item: (-item[1], item[0]))[:20]],
        "top_frames": [{"photo_id": photo["id"], "name": Path(photo["path"]).name, "score": photo["total_score"],
                        "burst": photo["burst"]} for photo in top],
    }


def _cell(value) -> str:
    return str(value).replace("|", "\\|")


def _table(headers: tuple[str, ...], rows: list) -> list[str]:
    return [f"| {' | '.join(headers)} |", f"|{' --- |' * len(headers)}",
            *(f"| {' | '.join(_cell(value) for value in row)} |" for row in rows)]


def render_facts_table(facts: dict) -> str:
    lines = [f"# Lumen digest {facts['date']}", ""]
    if facts["frames"] == 0:
        return "\n".join(lines + ["No frames were imported for this day.", ""])
    lines += _table(("Fact", "Count"), [("Frames", facts["frames"]), ("Keepers", facts["keepers"]),
                                        ("Rejects", facts["rejects"]), ("Bursts", facts["bursts"]),
                                        ("Astro jobs", len(facts["astro_jobs"]))])
    lines += ["", "## Keywords", ""]
    lines += _table(("Keyword", "Frames"), facts["keywords"]) if facts["keywords"] else ["No keywords were tagged."]
    lines += ["", "## Places", ""]
    lines += _table(("Place", "Frames"), facts["places"]) if facts["places"] else ["No places recorded."]
    lines += ["", "## Top frames", ""]
    lines += _table(("Frame", "Score", "Burst"), [
        (frame["name"], "unscored" if frame["score"] is None else f"{frame['score']:.2f}", frame["burst"])
        for frame in facts["top_frames"]]) if facts["top_frames"] else ["Every frame was rejected."]
    lines += ["", "## Astro", ""]
    lines += _table(("Job", "Status", "Lights"), [(job["job_id"], job["status"], job["lights"]) for job in facts["astro_jobs"]]
                    ) if facts["astro_jobs"] else ["No astro stacks were prepared this day."]
    return "\n".join(lines + [""])
