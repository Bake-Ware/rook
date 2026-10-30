"""concept.pool: what a project tagged with these concepts draws on."""

SIGNOFF = ("none", "experts", "user")          # loosest first
TRUST = ("ship-it", "normal", "sacred")


def _find(ctx, ref):
    by_slug = ctx.records.concept.list(where={"slug": ref}, limit=1)
    if by_slug:
        return by_slug[0]
    try:
        return ctx.records.concept.get(ref)
    except ctx.Error:
        return None


def pool(ctx, concepts):
    roles, skills, found, missing = [], [], [], []
    signoff, trust = "none", "ship-it"
    for ref in dict.fromkeys(concepts):
        c = _find(ctx, ref)
        if c is None or c["archived"]:
            missing.append(ref)
            continue
        roles += [r for r in c["roles"] or () if r not in roles]
        skills += [s for s in c["skills"] or () if s not in skills]
        signoff = max(signoff, c["signoff"] or "experts", key=SIGNOFF.index)
        trust = max(trust, c["trust"] or "normal", key=TRUST.index)
        found.append({"id": c["id"], "slug": c["slug"], "title": c["title"], "doc": c["doc"]})
    out = {"concepts": found, "roles": roles, "skills": skills,
           "signoff": signoff if found else None, "trust": trust if found else None}
    if missing:
        out["missing"] = missing
    return out
