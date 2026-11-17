"""Jenkins pipelines and the SonarQube quality gate, read-only.

Config:
  "ci":    {"type": "jenkins", "base_url": "https://jenkins.example.com",
            "job_path": "job/team/job/my-service",      # default multibranch job (optional)
            "sonar": "sonar", "sonar_project": "my-service"}   # optional link to a sonarqube instance
  "sonar": {"type": "sonarqube", "base_url": "https://sonar.example.com"}

Jenkins with an SSO realm (e.g. behind Google IAP) answers an expired session
with 403 and a meta-refresh to /securityRealm/commenceLogin; SonarQube answers
401 without its JWT-SESSION cookie. Each type recognizes its own answer, and
each instance recovers its own login.
"""
import html
import json
import re
import urllib.parse

from ..config import RUNTIME
from ..registry import B, Connector, I, S, connector_type, tool
from ..util import AuthExpired, ToolError, dumps, fit, looks_like_login

FAILED = ("FAILED", "FAILURE", "ERROR", "UNSTABLE", "ABORTED")


@connector_type("jenkins")
class Jenkins(Connector):
    probe_path = "/whoAmI/api/json"
    options = ("job_path", "sonar", "sonar_project")

    def is_expired(self, r):
        return (looks_like_login(r) or r.status == 401
                or (r.status == 403 and b"securityRealm/commenceLogin" in r.body[:3000]))

    def probe(self):
        try:
            r = self.request("GET", self.probe_path)
        except (AuthExpired, ToolError):
            return False
        try:
            me = json.loads(r.body)
        except ValueError:
            return False
        return bool(me.get("authenticated")) and me.get("name") not in (None, "anonymous")


@connector_type("sonarqube")
class SonarQube(Connector):
    probe_path = "/api/authentication/validate"
    login_path = "/sessions/new"

    def is_expired(self, r):
        return looks_like_login(r) or r.status in (401, 403)

    def probe(self):
        try:
            r = self.request("GET", self.probe_path)
        except (AuthExpired, ToolError):
            return False
        try:
            return bool(json.loads(r.body).get("valid"))
        except ValueError:
            return False


# ------------------------------------------------------------------ targeting
def parse_url(url):
    """Blue Ocean or classic Jenkins URL -> (job_path, pr, build).

    Blue Ocean: .../blue/organizations/jenkins/team%2Fmy-service/detail/PR-262/11/pipeline
    Classic:    .../job/team/job/my-service/job/PR-262/11/
    """
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    m = re.search(r"/blue/organizations/jenkins/([^/]+(?:/[^/]+)*?)/detail/PR-(\d+)(?:/(\d+))?", path)
    if m:
        return "/".join(f"job/{p}" for p in m.group(1).split("/")), m.group(2), m.group(3)
    m = re.search(r"/((?:job/[^/]+/)+?)job/PR-(\d+)(?:/(\d+))?", path)
    if m:
        return m.group(1).strip("/"), m.group(2), m.group(3)
    raise ToolError(f"cannot find a PR in the Jenkins URL: {url}")


def target(j, pr=None, url=None, build=None, job_path=None):
    url_job = url_build = None
    if url:
        url_job, pr_from_url, url_build = parse_url(url)
        pr = pr or pr_from_url
    if not pr:
        raise ToolError("pass pr (e.g. 262) or a Jenkins url.")
    pr = str(pr).strip().upper().replace("PR-", "").lstrip("#")
    if not pr.isdigit():
        raise ToolError(f"invalid PR number: {pr!r}")
    job = (job_path or url_job or j.cfg.get("job_path") or "").strip("/")
    if not job:
        raise ToolError(f"{j.name}: no job_path. Pass job_path (e.g. 'job/team/job/my-service'), a "
                        "Jenkins URL, or set job_path on the connector.")
    build = str(build or url_build or "").strip()
    if not build:
        build = j.call("GET", f"/{job}/job/PR-{pr}/lastBuild/buildNumber").text().strip()
    return job, pr, build


def _blue_url(j, job, pr, build):
    pipeline = "%2F".join(p for p in job.split("/") if p and p != "job")
    return f"{j.base_url}/blue/organizations/jenkins/{pipeline}/detail/PR-{pr}/{build}/pipeline"


def _num(b):
    return int(b) if str(b).isdigit() else b


def quality_gate(sonar, project, pull_request=None, branch=None):
    q = {"projectKey": project}
    if pull_request:
        q["pullRequest"] = pull_request
    elif branch:
        q["branch"] = branch
    ps = sonar.get_json("/api/qualitygates/project_status?" + urllib.parse.urlencode(q)).get("projectStatus", {})
    cmp = {"GT": ">", "LT": "<", "GTE": ">=", "LTE": "<="}
    conditions = [{
        "metric": c.get("metricKey"), "status": c.get("status"), "actual": c.get("actualValue"),
        "fails_if": f"{c.get('metricKey')} {cmp.get(c.get('comparator'), c.get('comparator'))} "
                    f"{c.get('errorThreshold')}",
    } for c in ps.get("conditions", [])]
    dash = {"id": project, **({"pullRequest": pull_request} if pull_request else {}),
            **({"branch": branch} if branch and not pull_request else {})}
    return {"project": project, "status": ps.get("status"),
            "url": f"{sonar.base_url}/dashboard?" + urllib.parse.urlencode(dash),
            "failing": [c for c in conditions if (c["status"] or "").upper() == "ERROR"],
            "conditions": conditions}


# ----------------------------------------------------------------------- tools
TARGET_PROPS = {
    "pr": {"type": ["integer", "string"], "description": "PR number, e.g. 262."},
    "url": S("Jenkins URL instead of pr/build/job_path: Blue Ocean "
             "(.../detail/PR-262/11/pipeline) or classic (.../job/PR-262/11/)."),
    "build": {"type": ["integer", "string"], "description": "Build number (default: the PR's latest)."},
    "job_path": S("Multibranch job path, e.g. 'job/team/job/my-service' (default: the connector's job_path)."),
}


@tool("jenkins_pr_status", "Check a PR's CI",
      "A pull request's Jenkins pipeline: every stage with status and duration, the log tail of each "
      "failed step, and, when the connector links a SonarQube instance, the quality gate with the "
      "failing conditions (Jenkins alone only reports the gate as ERROR).",
      dict(TARGET_PROPS, **{
          "sonar_project": S("SonarQube project key (default: the connector's sonar_project)."),
          "log_lines": I("Lines of each failed step's log to return (default 80).", default=80),
          "skip_sonar": B("Only report the Jenkins stages.")}),
      type="jenkins")
def jenkins_pr_status(j, pr=None, url=None, build=None, job_path=None, sonar_project=None,
                      log_lines=80, skip_sonar=False):
    job, pr, build = target(j, pr, url, build, job_path)
    base = f"/{job}/job/PR-{pr}/{build}"
    run = j.get_json(f"{base}/wfapi/describe")
    stages, failed = [], []
    for st in run.get("stages", []):
        status = st.get("status", "N/A")
        stages.append({"name": st.get("name"), "status": status,
                       "seconds": round(st.get("durationMillis", 0) / 1000.0, 1)})
        if (status or "").upper() in FAILED:
            failed.append(st)

    failures = []
    for st in failed:
        entry = {"stage": st.get("name"), "steps": []}
        link = (st.get("_links") or {}).get("self", {}).get("href")
        if link:
            for node in j.get_json(link).get("stageFlowNodes", []):
                if (node.get("status") or "").upper() not in FAILED:
                    continue
                step = {"step": node.get("name"), "status": node.get("status")}
                log_href = (node.get("_links") or {}).get("log", {}).get("href")
                if log_href:
                    try:
                        log = j.get_json(log_href)
                        text = log.get("text", "") if isinstance(log, dict) else str(log)
                        # wfapi log text is HTML: console-note anchors plus escaped entities.
                        text = html.unescape(re.sub(r"<[^>]+>", "", text))
                        step["log_tail"] = "\n".join(text.splitlines()[-log_lines:])
                    except ToolError as e:
                        step["log_error"] = str(e)
                entry["steps"].append(step)
        failures.append(entry)

    out = {"instance": j.name, "pr": int(pr), "build": _num(build), "job_path": job,
           "overall": run.get("status"), "url": _blue_url(j, job, pr, build), "stages": stages}
    if failures:
        out["failed_stages"] = failures
    sonar_name = j.cfg.get("sonar")
    project = sonar_project or j.cfg.get("sonar_project")
    if not skip_sonar and sonar_name and project:
        try:
            sonar = RUNTIME.resolve("sonarqube", sonar_name)
            out["quality_gate"] = quality_gate(sonar, project, pull_request=pr)
        except ToolError as e:
            out["quality_gate"] = {"project": project, "error": str(e)}
    return fit(dumps(out), "jenkins-pr")


@tool("jenkins_console_log", "Read a Jenkins console log",
      "A PR build's console output: the last `tail` lines, or the lines matching `grep` (with line "
      "numbers and context). Use when a failed step's log tail from jenkins_pr_status is not enough.",
      dict(TARGET_PROPS, **{
          "tail": I("Lines to return (default 200).", default=200),
          "grep": S("Case-insensitive regex, e.g. 'FAIL|panic|error:'."),
          "context": I("Lines of context around each grep match (default 2).", default=2)}),
      type="jenkins")
def jenkins_console_log(j, pr=None, url=None, build=None, job_path=None, tail=200, grep=None, context=2):
    job, pr, build = target(j, pr, url, build, job_path)
    lines = j.call("GET", f"/{job}/job/PR-{pr}/{build}/consoleText", timeout=120).text().splitlines()
    head = {"instance": j.name, "pr": int(pr), "build": _num(build), "job_path": job,
            "total_lines": len(lines), "url": f"{j.base_url}/{job}/job/PR-{pr}/{build}/console"}
    if not grep:
        return fit(dumps(head) + "\n\n" + "\n".join(lines[-tail:]), "jenkins-console")
    try:
        rx = re.compile(grep, re.I)
    except re.error as e:
        raise ToolError(f"invalid grep regex: {e}")
    hits = [i for i, line in enumerate(lines) if rx.search(line)]
    keep = set()
    for i in hits:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    picked, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            picked.append("--")
        picked.append(f"{i + 1}: {lines[i]}")
        prev = i
    head["matches"] = len(hits)
    body = "\n".join(picked[-tail:]) if picked else "(no matching lines)"
    return fit(dumps(head) + "\n\n" + body, "jenkins-console")


@tool("sonar_quality_gate", "SonarQube quality gate",
      "A project's quality-gate status and conditions for a pull request, a branch, or the main branch, "
      "with the failing conditions listed first.",
      {"project": S("Project key."),
       "pull_request": {"type": ["integer", "string"], "description": "Pull request number."},
       "branch": S("Branch name (ignored when pull_request is given).")},
      required=["project"], type="sonarqube")
def sonar_quality_gate(s, project, pull_request=None, branch=None):
    return dumps(quality_gate(s, project, str(pull_request) if pull_request else None, branch))
