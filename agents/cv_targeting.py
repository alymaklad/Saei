"""
Which of the job's words the tailored CV should use, and where.

The problem this solves
-----------------------
A CV can describe exactly the right work in the wrong vocabulary. The job asks
for "Deep Learning" and the CV says "CNN"; the job asks for "SQL" and the CV
says "PostgreSQL"; the job says "Large Language Models (LLM)" and the CV only
ever writes "LLM". The candidate qualifies in every one of those cases, and
the scorer knows it -- agents/skill_matching.py's HYPONYMS table is what makes
"CNN" count towards a Deep Learning requirement in the first place.

But it counts at a DISCOUNT. From config.py:

    exact or alias, demonstrated   1.00   used the job's own word, in real work
    subset, demonstrated           0.80   did something that is a kind of it
    exact or alias, listed only    0.65   named it, no context
    subset, listed only            0.52   named something adjacent, no context

So a CV saying "Trained a CNN" earns 80% of a Deep Learning requirement, and
the same sentence written as "Trained a CNN for deep-learning image
classification" earns 100%. Nothing was invented: a CNN IS deep learning, and
the entailment table already asserts exactly that. The points were being left
on the table by wording alone.

Why the targets are computed here and not asked of the model
------------------------------------------------------------
The obvious alternative is to hand the model the job description and say "use
its terminology". That produces the fabrication this pipeline exists to
prevent: a model told to match a job's vocabulary will write "Kubernetes" into
a CV that only ever used Docker, because the words sit next to each other in
its training data. Kubernetes and Docker are SIBLINGS -- skill_matching's
EXCLUSIVE_GROUPS exists because that exact substitution is what an
unconstrained matcher makes.

So the bridges are enumerated from the tables instead, and each one names the
entry that holds the supporting evidence. The model is not asked which of the
job's words apply; it is told which ones apply, where, and what is already
written there. Anything it introduces beyond that list is caught in
cv_rewriter_agent.build_document, which re-checks every introduced term
against that entry's own source text.

A target is therefore never a claim the CV could not already defend. It is the
same claim, in the reader's vocabulary.
"""
import re

from agents import cv_profile, skill_matching

# The profile as one searchable string. Lives in cv_profile because the
# ranking stage needs it too -- see agents/ranking_agent.py.
profile_text = cv_profile.profile_text

# The job's own wording only helps where the scorer can see it, and prompt
# space is not free -- a run with forty requirements would otherwise bury the
# handful of bridges that actually move the number.
MAX_TARGETS = 12

# Below this, a requirement is already earning what it can and adding its word
# again changes nothing. 0.999 rather than 1.0 to survive the rounding in
# ats_agent._outcome.
FULL_CREDIT = 0.999

_IMPORTANCE_RANK = {"required": 0, "must_have": 0, "preferred": 1, "nice_to_have": 2}


def _entry_text(entry: dict, *fields: str) -> str:
    """Everything a matcher would read in one entry: its heading and bullets.

    The same text the scorer's spans are built from (see
    cv_profile.evidence_spans), so a term found here is a term the scorer
    would find too.
    """
    parts = [str(entry.get(f) or "") for f in fields]
    parts.extend(str(b or "") for b in entry.get("bullets") or [])
    return " \n".join(p for p in parts if p)


def _bullet_text(entry: dict) -> str:
    return " \n".join(str(b or "") for b in entry.get("bullets") or [] if str(b or "").strip())


def _entries(profile: dict):
    """(section, index, label, full_text, bullet_text) per entry.

    The two texts are kept apart because a term in a heading and a term in a
    bullet are not equally useful to write against: both are evidence to the
    scorer, but only the bullet is a sentence describing work.
    """
    for index, entry in enumerate(profile.get("experience") or []):
        if isinstance(entry, dict):
            label = entry.get("title") or entry.get("organization") or f"experience[{index}]"
            yield ("experience", index, label,
                   _entry_text(entry, "title", "organization"), _bullet_text(entry))
    for index, entry in enumerate(profile.get("projects") or []):
        if isinstance(entry, dict):
            label = entry.get("name") or f"projects[{index}]"
            yield "projects", index, label, _entry_text(entry, "name"), _bullet_text(entry)


def unsupported_by_profile(terms, profile: dict) -> set:
    """Which of `terms` the profile genuinely has no evidence for.

    `missing_skills` comes from scoring the UPLOADED CV, so it answers "your
    CV file shows no sign of this". Used unchanged as a block list against the
    profile, it would overrule the user's own edits: add Kubernetes to the role
    that used it on the Profile page, and a rewrite would still strip it out
    because a stale file never mentioned it. Narrowing it here keeps the guard
    (a skill neither document supports is still refused) without letting the
    file veto the record.
    """
    text = profile_text(profile)
    return {skill_matching.canonical(t) for t in (terms or [])
            if str(t).strip() and not is_supported(str(t), text)}


def supporting_terms(requirement: str) -> list[str]:
    """Terms whose presence would let `requirement` be claimed honestly.

    Two curated relations, in the order ats_agent.match_requirement tries
    them, so this can never propose a bridge the scorer would refuse:

      hyponym    the entry's word is a KIND of the requirement -- "CNN" for
                 Deep Learning. Currently scored at subset credit (0.80).
      prerequisite  the entry's word cannot be used WITHOUT the requirement --
                 "FastAPI" for Python, "PostgreSQL" for SQL. Scored at 0.90.

    Both are worth bridging even though neither is wrong as it stands: the
    difference to exact credit is 0.20 and 0.10 respectively, and more to the
    point, the ATS on the other side is probably matching strings, and a
    keyword filter looking for "Python" does not know what FastAPI is.
    Hyponyms come first because a narrower-to-broader bridge is the more
    natural sentence to write; both are already exclusivity-guarded.
    """
    canon = skill_matching.canonical(requirement)
    terms = [
        term for term in skill_matching.HYPONYMS.get(canon, ())
        if skill_matching.entails(canon, term)
    ]
    seen = {skill_matching.canonical(t) for t in terms}
    for term in skill_matching.implying_terms(requirement):
        if skill_matching.canonical(term) not in seen:
            terms.append(term)
            seen.add(skill_matching.canonical(term))
    return terms


def _is_initialism(short: str, phrase: str) -> bool:
    """'llm' for 'large language model' -- yes. 'cicd' for 'ci/cd' -- no.

    Guards the expansion hint below. Without it, any short alias would be
    offered as an acronym and the CV would sprout parentheses around synonyms
    that are not acronyms at all.
    """
    words = [w for w in re.split(r"[\s/-]+", phrase) if w]
    return len(words) > 1 and "".join(w[0] for w in words).casefold() == short.casefold()


def expansion_hint(term: str) -> str | None:
    """'LLM' -> 'Large Language Models (LLM)', when the pair is a real acronym.

    Written once in full and by acronym everywhere else, which is how a human
    writes it and what a literal keyword matcher on the other side needs: an
    ATS searching for the spelled-out phrase never finds a CV that only ever
    says "LLM", and vice versa.
    """
    canon = skill_matching.canonical(term)
    if " " not in canon:
        return None
    for alias in skill_matching.ALIASES.get(canon, []):
        if _is_initialism(alias, canon):
            return f"{canon.title()} ({alias.upper()})"
    return None


def _rows_from(requirement_results, required_skills) -> list[dict]:
    """Normalise both engines' output into rows this module can read.

    The requirements engine reports per-requirement credit; the legacy engine
    reports a flat list of skill names. Rows from the latter carry no credit,
    which just means every one of them is considered.
    """
    if requirement_results:
        return [dict(row) for row in requirement_results if isinstance(row, dict)]
    return [{"name": str(name), "importance": "required"}
            for name in (required_skills or []) if str(name).strip()]


def _locate(name: str, entries) -> dict:
    """Where, if anywhere, one requirement is already supported.

    Three distinct answers, and the difference between them is the whole
    design: the entry that uses the job's OWN word needs nothing; an entry
    that uses a synonym or acronym may want both forms written out; an entry
    that uses something the job's word covers is where a bridge belongs.
    """
    found = {"exact": None, "alias": None, "alias_form": None,
             "support": None, "support_form": None, "support_rank": 0,
             "support_relation": None}
    support = supporting_terms(name)

    for section, index, label, text, bullets in entries:
        hit = skill_matching.find_term(name, text)
        if hit:
            where = (section, index, label)
            if skill_matching.normalize(hit) == skill_matching.normalize(name):
                found["exact"] = found["exact"] or where
            elif not found["alias"]:
                found["alias"], found["alias_form"] = where, hit
            continue
        # Support found in a BULLET beats support found only in the entry's
        # heading. Both are real evidence to the scorer, but a heading is a
        # name -- "Master AI Agents & LLMs" -- and the bullet that describes
        # the work is where the job's word belongs in a sentence.
        for term in support:
            sub = skill_matching.find_term(term, text)
            if not sub:
                continue
            rank = 2 if skill_matching.find_term(term, bullets) else 1
            if rank > found["support_rank"]:
                found["support"], found["support_form"] = (section, index, label), sub
                found["support_rank"] = rank
                found["support_relation"] = (
                    "prerequisite" if skill_matching.implied_by(name, term) else "hyponym")
            break
    return found


def build_targets(profile: dict, *, requirement_results=None, required_skills=None,
                  missing_skills=None, limit: int = MAX_TARGETS) -> list[dict]:
    """The job's words this CV has already earned, and the entry that earned them.

    Two kinds of target come out of this:

      bridge  the entry describes the work in a word the job does not use,
              and the entailment tables say the job's word is already true of
              it -- either because the entry's word is a KIND of it ("CNN"
              for Deep Learning, 0.80) or because the work could not have
              been done WITHOUT it ("FastAPI" for Python, "PostgreSQL" for
              SQL, 0.90). Writing both closes the gap to exact credit and
              claims nothing new; it also puts the literal keyword in front
              of the ATS on the other side, which is not reading an
              entailment table.

      form    the entry and the job use the same term in different surface
              forms, one of them an acronym ("LLM" vs "Large Language
              Models"). This one is worth nothing to THIS scorer, which
              already treats them as aliases; it is for the ATS on the other
              side, which may well be doing literal string matching.

    Ordered by what they are worth: required before preferred, and within
    that, the requirements currently losing the most credit first.
    """
    profile = profile if isinstance(profile, dict) else {}
    # Narrowed against the profile: a requirement the CV file missed but the
    # profile demonstrates is a legitimate target, not a fabrication.
    blocked = unsupported_by_profile(missing_skills, profile)
    entries = list(_entries(profile))

    targets, seen = [], set()
    for row in _sorted_rows(_rows_from(requirement_results, required_skills)):
        name = str(row.get("name") or "").strip()
        if not name or len(targets) >= limit:
            continue

        canon = skill_matching.canonical(name)
        if canon in seen or canon in blocked:
            # blocked: the scoring pass found no evidence for this one, so
            # asking for its wording is asking for a lie.
            continue
        if str(row.get("relation") or "") == "none":
            continue

        found = _locate(name, entries)
        credit = float(row.get("credit") or 0.0)
        target = None

        if credit < FULL_CREDIT and not found["exact"] and found["support"]:
            section, index, label = found["support"]
            target = {"kind": "bridge", "evidence_term": found["support_form"],
                      "relation": found["support_relation"] or "hyponym",
                      "section": section, "ref": index, "entry": label}
        elif found["alias"] and not found["exact"]:
            section, index, label = found["alias"]
            pair = _acronym_pair(name, found["alias_form"])
            if pair:
                target = {"kind": "form", "evidence_term": found["alias_form"],
                          "section": section, "ref": index, "entry": label,
                          "expansion": pair}

        if not target:
            continue
        seen.add(canon)
        targets.append({
            "term": name, "canonical": canon,
            "importance": str(row.get("importance") or "required"),
            "credit_now": round(credit, 2),
            "expansion": target.pop("expansion", None),
            **target,
        })

    return targets


def _acronym_pair(requirement: str, cv_form: str) -> str | None:
    """"Large Language Models (LLM)" when the job and the CV use the two
    halves of an acronym pair. None when they are just different words --
    "Postgres" for "PostgreSQL" is a synonym, and spelling both out on a CV
    reads as padding rather than coverage."""
    long_form = skill_matching.canonical(requirement)
    short = skill_matching.normalize(cv_form)
    if short == skill_matching.normalize(requirement):
        return None
    if _is_initialism(short, long_form):
        return f"{long_form.title()} ({short.upper()})"
    if _is_initialism(skill_matching.normalize(requirement), long_form):
        # The job used the acronym and the CV spells it out.
        return f"{long_form.title()} ({skill_matching.normalize(requirement).upper()})"
    return None


def listed_only(profile: dict, requirement_results=None) -> list[str]:
    """Requirements the CV only NAMES, with no entry to attach them to.

    Not targets -- there is nothing here that can be written truthfully by a
    rewrite, because no role or project on record mentions the skill, anything
    that is a kind of it, or anything that requires it. Python listed with a
    FastAPI bullet somewhere is NOT in this list; Python listed with no code
    on record is. Worth reporting to the user, though: if they did
    use PyTorch in that internship, saying so on the Profile page is worth 35%
    of that requirement, and only they know whether it is true.
    """
    entries = list(_entries(profile))
    out = []
    for row in requirement_results or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("evidence_location")) != "claimed":
            continue
        name = str(row.get("name") or "").strip()
        if name and not _locate(name, entries)["support"]:
            out.append(name)
    return out


def _sorted_rows(rows: list[dict]) -> list[dict]:
    def rank(row):
        importance = _IMPORTANCE_RANK.get(str(row.get("importance") or "").lower(), 1)
        # Most credit left on the table first.
        return (importance, float(row.get("credit") or 0.0))
    return sorted(rows, key=rank)


def format_targets(targets: list[dict]) -> str:
    """The target list as prompt text.

    Names the entry by section and index, because that is how the model
    addresses entries in its reply -- a target it cannot locate is a target it
    will place somewhere convenient instead.
    """
    lines = []
    for target in targets:
        where = f'{target["section"]}[{target["ref"]}] ({target["entry"]})'
        if target["kind"] == "form":
            lines.append(
                f'- {where} writes "{target["evidence_term"]}"; the job writes it out. '
                f'Give it in full ONCE as "{target["expansion"]}", and keep the short '
                f'form everywhere else it appears.'
            )
        elif target.get("relation") == "prerequisite":
            lines.append(
                f'- {where} says "{target["evidence_term"]}", which is not used '
                f'without "{target["term"]}". Keep "{target["evidence_term"]}" and name '
                f'"{target["term"]}" in the same bullet as the thing it was built with.'
            )
        else:
            lines.append(
                f'- {where} says "{target["evidence_term"]}", which is a kind of '
                f'"{target["term"]}". Keep "{target["evidence_term"]}" and work the '
                f'job\'s own wording "{target["term"]}" into the same bullet.'
            )
    return "\n".join(lines)


def introduced_terms(bullets, source_text: str, candidates) -> list[str]:
    """Candidate terms that appear in the rewritten bullets but not the source.

    The verification counterpart to build_targets: what the model actually
    added to an entry, whether or not it was asked to.
    """
    written = " \n".join(str(b or "") for b in bullets or [])
    out = []
    for term in candidates:
        if not str(term or "").strip():
            continue
        if skill_matching.find_term(term, written) and not skill_matching.find_term(term, source_text):
            out.append(str(term))
    return out


def is_supported(term: str, source_text: str) -> bool:
    """Would the scorer accept this term against this entry's own evidence?

    True when the entry already uses the term (or a synonym), or uses
    something the entailment table says is a kind of it. This is the check
    that separates "CNN, so deep learning" from "Docker, so Kubernetes" --
    the second is a sibling, and skill_matching.entails refuses it.
    """
    if skill_matching.find_term(term, source_text):
        return True
    return any(skill_matching.find_term(support, source_text)
               for support in supporting_terms(term))


def coverage(targets: list[dict], document: dict) -> dict:
    """Which targets actually made it into the tailored CV.

    Reported rather than enforced: a target the model declined to use is a
    judgement call about a sentence, not a failure. But an empty coverage list
    means the tailoring did nothing the scorer will notice, and that is worth
    seeing in the bench.
    """
    used, missed = [], []
    for target in targets:
        entries = document.get(target["section"]) or []
        landed = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = " \n".join(str(b or "") for b in entry.get("bullets") or [])
            if skill_matching.find_term(target["evidence_term"], text) or \
               skill_matching.find_term(target["term"], text):
                landed = bool(skill_matching.find_term(target["term"], text))
                if landed:
                    break
        (used if landed else missed).append(target["term"])
    return {"used": used, "missed": missed,
            "rate": round(len(used) / len(targets), 2) if targets else None}
