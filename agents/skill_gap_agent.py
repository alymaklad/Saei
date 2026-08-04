"""Skill Gap Agent — surfaces the most common missing skills across recent job scores."""
from collections import Counter


def summarize_skill_gaps(job_scores: list[dict]) -> str:
    if not job_scores:
        return "No jobs scored yet — nothing to report."
    all_missing = []
    for j in job_scores:
        all_missing.extend(j.get("missing_skills", []))
    if not all_missing:
        return "No recurring skill gaps found in recent jobs reviewed."
    counts = Counter(all_missing)
    top_gaps = counts.most_common(10)
    lines = [f"- {skill}: missing in {count} of {len(job_scores)} jobs reviewed" for skill, count in top_gaps]
    return "Top skill gaps for your field:\n" + "\n".join(lines)
