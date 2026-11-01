"""Find GSC bulk exports in BigQuery: every dataset (in every project the
account can see) that holds a `searchdata_url_impression` table.

Uses only metadata calls (no queries, no cost).
"""

from __future__ import annotations

from typing import Callable

EXPORT_TABLE = "searchdata_url_impression"


def _service(creds):
    from googleapiclient.discovery import build

    return build("bigquery", "v2", credentials=creds, cache_discovery=False)


def _paged(request_fn, next_fn, key: str):
    req = request_fn()
    while req is not None:
        resp = req.execute()
        yield from resp.get(key, [])
        req = next_fn(req, resp)


def list_gsc_exports(
    creds,
    progress: Callable[[str], None] | None = None,
    max_projects: int = 300,
    svc=None,
) -> list[dict]:
    """Return [{project, dataset, table, rows, location}] sorted by project."""
    from googleapiclient.errors import HttpError

    svc = svc or _service(creds)
    projects = [
        p["projectReference"]["projectId"]
        for p in _paged(lambda: svc.projects().list(maxResults=1000), svc.projects().list_next, "projects")
    ][:max_projects]

    found = []
    for i, project in enumerate(projects, 1):
        if progress:
            progress(f"BigQuery doorzoeken: project {i} van {len(projects)} ({project})")
        try:
            datasets = list(_paged(
                lambda: svc.datasets().list(projectId=project, maxResults=1000),
                svc.datasets().list_next, "datasets",
            ))
        except HttpError:
            continue
        for ds in datasets:
            dataset = ds["datasetReference"]["datasetId"]
            try:
                t = svc.tables().get(projectId=project, datasetId=dataset, tableId=EXPORT_TABLE).execute()
            except HttpError:
                continue
            found.append({
                "project": project,
                "dataset": dataset,
                "table": f"{project}.{dataset}.{EXPORT_TABLE}",
                "rows": int(t.get("numRows", 0) or 0),
                "location": ds.get("location", ""),
            })
    return sorted(found, key=lambda d: (d["project"], d["dataset"]))
