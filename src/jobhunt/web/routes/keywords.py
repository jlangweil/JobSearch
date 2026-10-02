from __future__ import annotations

from fastapi import APIRouter, Request

from jobhunt.db import queries
from jobhunt.discovery import keyword_heat
from jobhunt.web.common import redirect, render, rt

router = APIRouter()


@router.get("/keywords")
def keywords_page(request: Request):  # noqa: ANN201
    r = rt()
    tech = queries.keyword_heat(r.engine, "technology")
    comps = queries.keyword_heat(r.engine, "competency")
    top = max([row["jobs"] for row in tech + comps] or [1])
    gaps = [row for row in tech + comps if row["share"] >= 0.1 and row["have"] * 2 < row["jobs"]]
    gaps.sort(key=lambda row: (-row["jobs"], row["label"].lower()))
    return render(request, "keywords.html", {"tech": tech[:80], "comps": comps[:40], "top": top, "gaps": gaps[:15],
                                             "meta": keyword_heat.meta(r.engine),
                                             "min_score": r.cfg.scoring.heatmap_min_score})


@router.post("/keywords/rebuild")
def rebuild_keywords():  # noqa: ANN201
    meta = keyword_heat.rebuild(rt().engine, rt().cfg)
    return redirect("/keywords", message=f"Recounted {meta['keywords']} keywords across {meta['jobs']} jobs.")
