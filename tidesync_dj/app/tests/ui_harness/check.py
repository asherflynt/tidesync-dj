"""Drive the dashboard in Chrome at phone, tablet and laptop sizes and report problems.

    cd tidesync_dj/app
    python tests/ui_harness/check.py [--shots DIR]

Starts serve.py (in-memory Music Assistant, no network), then for each size
walks the main flows (Start, vibe chip, Calmer/Hotter, block + Undo, editing
a listener, tapping a search result, Stop) and audits each screen: horizontal
overflow, console errors, tap targets under 44x44 px, form fields under 16 px
and text under 13 px. Screenshots go to DIR (default ~/.cache/tidesync-shots).
Needs Playwright (pip install playwright) and Chrome installed.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
SIZES = {
    "390x844": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
                "device_scale_factor": 3},
    "820x1180": {"viewport": {"width": 820, "height": 1180}, "has_touch": True, "device_scale_factor": 2},
    "1280x800": {"viewport": {"width": 1280, "height": 800}},
}

AUDIT_JS = """() => {
  const shown = el => {
    if (!el.getClientRects().length) return false;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden";
  };
  const name = el => (el.id ? "#" + el.id : el.tagName.toLowerCase() + "." + [...el.classList].join("."))
    + " '" + (el.getAttribute("aria-label") || el.textContent || el.value || "").trim().slice(0, 24) + "'";
  const W = document.documentElement.clientWidth;
  const targets = [...document.querySelectorAll(
    "button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button]")].filter(shown);
  const small = targets.map(el => [el, el.getBoundingClientRect()])
    .filter(([, r]) => r.width < 44 - 0.5 || r.height < 44 - 0.5)
    .map(([el, r]) => `${name(el)} ${Math.round(r.width)}x${Math.round(r.height)}`);
  const fields = [...document.querySelectorAll("input:not([type=range]):not([type=hidden]), select, textarea")]
    .filter(shown);
  const smallFields = fields.filter(el => parseFloat(getComputedStyle(el).fontSize) < 16)
    .map(el => `${name(el)} ${getComputedStyle(el).fontSize}`);
  const wide = [...document.querySelectorAll("body *")].filter(shown)
    .filter(el => el.getBoundingClientRect().right > W + 1 || el.getBoundingClientRect().left < -1)
    .filter(el => !el.closest(".toast"))
    .map(name);
  const scrollers = [...document.querySelectorAll(".panel, .group, .panels, .app, body, html")]
    .filter(el => el.scrollWidth > el.clientWidth + 1).map(name);
  let minFont = 99, smallText = [];
  for (const el of document.querySelectorAll("body *")) {
    if (!shown(el) || el.closest("svg")) continue;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    minFont = Math.min(minFont, size);
    if (size < 13) smallText.push(`${name(el)} ${size}px`);
  }
  return {targets: targets.length, small, fields: fields.length, smallFields,
          overflow: [...new Set([...wide, ...scrollers])], minFont, smallText,
          prompts: /\\b(prompt|confirm|alert)\\(/.test(document.documentElement.innerHTML)};
}"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}/"
        self.proc = subprocess.Popen([sys.executable, str(HERE / "serve.py"), "--port", str(self.port)],
                                     cwd=HERE.parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "status", timeout=1)
                return
            except OSError:
                time.sleep(0.25)
        raise SystemExit("harness did not start: " + self.proc.stderr.read().decode()[-2000:])

    def post(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")

    def get(self, path: str) -> dict:
        return json.loads(urllib.request.urlopen(self.base + path, timeout=10).read())

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)


class Run:
    """One browser size: flows, audits and screenshots."""

    def __init__(self, h: Harness, page: Page, size: str, shots: Path) -> None:
        self.h, self.page, self.size, self.shots = h, page, size, shots
        self.phone = size.startswith("390")
        self.errors: list[str] = []
        self.failures: list[str] = []
        self.audits: dict[str, dict] = {}
        self.steps: list[str] = []
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: (self.failures.append(f"native dialog: {d.message}"), d.dismiss()))

    def tab(self, name: str) -> None:
        if self.phone:
            self.page.locator(f'.navbtn[data-tab="{name}"]').tap()
            self.page.wait_for_timeout(150)

    def tap(self, selector: str) -> None:
        loc = self.page.locator(selector).first
        loc.scroll_into_view_if_needed()
        loc.tap() if self.phone or self.size.startswith("820") else loc.click()

    def audit(self, state: str, *, open_details: bool = False) -> None:
        if open_details:
            self.page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
            self.page.wait_for_timeout(100)
        self.audits[state] = self.page.evaluate(AUDIT_JS)
        self.page.screenshot(path=str(self.shots / f"{self.size}-{state}.png"))
        if open_details:
            self.page.evaluate("document.querySelectorAll('details').forEach(d => d.open = false)")

    def step(self, name: str, fn) -> None:
        try:
            fn()
            self.steps.append(name)
        except Exception as err:  # noqa: BLE001 - report and carry on with the next flow
            self.failures.append(f"{name}: {type(err).__name__}: {str(err).splitlines()[0][:160]}")

    def wait(self, js: str, timeout: int = 8000) -> None:
        self.page.wait_for_function(js, timeout=timeout)

    # -- flows -------------------------------------------------------------

    def start(self) -> None:
        self.wait("!document.getElementById('npStart').hidden")
        self.audit("stopped")
        self.tap("#startRadio")
        self.wait("!document.getElementById('npPlaying').hidden && "
                  "document.getElementById('nowPlaying').textContent.includes(' - ')")
        self.wait("!document.getElementById('stopBtn').hidden")
        self.page.wait_for_timeout(300)
        self.audit("playing")

    def vibe_chip(self) -> None:
        self.tap('.vibe-chip[data-vibe^="dinner"]')
        self.wait("document.getElementById('vibePill').textContent.startsWith('dinner')")
        self.wait("document.querySelector('.vibe-chip[data-vibe^=\"dinner\"]').classList.contains('active')")
        assert self.h.get("status")["vibe"].startswith("dinner")

    def energy(self) -> None:
        before = self.h.get("status")["energy_bias"]
        self.tap("#energyUp")
        self.wait("!document.getElementById('energyUp').disabled")
        self.page.wait_for_timeout(200)
        mid = self.h.get("status")["energy_bias"]
        assert mid == before + 1, (before, mid)
        self.tap("#energyDown")
        self.wait("!document.getElementById('energyDown').disabled")
        self.page.wait_for_timeout(200)
        assert self.h.get("status")["energy_bias"] == before
        for sel, label in (("#energyDown", "Calmer"), ("#energyUp", "Hotter")):
            assert label in self.page.locator(sel).inner_text(), sel

    def block_undo(self) -> None:
        playing = self.h.get("status")["now_playing"]
        self.tap("#blockBtn")
        self.wait("document.getElementById('toast').classList.contains('show') && "
                  "!document.getElementById('toastAction').hidden")
        self.page.wait_for_timeout(350)  # let the toast finish fading in
        self.audit("blocked-toast")
        assert self.h.get("status")["now_playing"] != playing
        blocked = [p for p in self.h.get("users")["people"] if p["active"]]
        self.tap("#toastAction")
        self.wait(f"document.getElementById('nowPlaying').textContent === {json.dumps(playing)}")
        assert blocked and self.h.get("status")["now_playing"] == playing

    def edit_listener(self) -> None:
        names = [p["name"] for p in self.h.get("users")["people"]]
        assert "Listener Profile" in names
        self.tap("#editPeople")
        self.wait("document.getElementById('sheet').classList.contains('show')")
        self.page.wait_for_timeout(250)
        self.audit("edit-listeners")
        field = self.page.locator('#sheet input[data-slug="dad"]')
        field.fill("Papa")
        self.tap("#sheetSave")
        self.wait("!document.getElementById('sheet').classList.contains('show')")
        self.wait("[...document.querySelectorAll('#peopleBtns .person')].some(b => b.textContent === 'Papa')")
        assert "Papa" in [p["name"] for p in self.h.get("users")["people"]]
        assert "Listener Profile" in [p["name"] for p in self.h.get("users")["people"]]
        self.h.post("users/rename", {"slug": "dad", "name": "Dad"})

    def search_play_next(self) -> None:
        self.tab("queue")
        field = self.page.locator("#searchInput")
        field.scroll_into_view_if_needed()
        field.fill("velvet")
        self.wait("document.querySelectorAll('#searchResults .rowmain').length > 0")
        self.audit("search")
        row = self.page.locator("#searchResults .rowmain").first
        title = row.locator(".t").inner_text()
        self.tap("#searchResults .rowmain")
        self.wait("document.getElementById('toast').classList.contains('show')")
        self.page.wait_for_timeout(400)
        queue = self.h.get("queue")["items"]
        assert queue[1]["name"] == title, (title, [q["name"] for q in queue[:3]])
        self.tap("#searchResults .rowbtn.more")
        self.wait("document.getElementById('sheet').classList.contains('show')")
        self.audit("search-menu")
        self.tap("#sheetCancel")
        self.wait("!document.getElementById('sheet').classList.contains('show')")
        self.page.locator("#searchInput").fill("")
        self.tab("np")

    def settings(self) -> None:
        self.tab("settings")
        self.page.wait_for_timeout(200)
        self.audit("settings", open_details=True)
        self.tab("np")

    def stop(self) -> None:
        self.tap("#stopBtn")
        self.wait("!document.getElementById('npStart').hidden")

    def library_states(self) -> None:
        for name in ("measuring", "unavailable", "eta"):
            self.h.post("__harness/scenario", {"library": name})
            self.page.evaluate("refreshStatus()")
            self.wait("!document.getElementById('readyCard').hidden")
            self.page.locator("#readyCard").scroll_into_view_if_needed()
            self.audit(f"ready-{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shots", default=str(Path.home() / ".cache" / "tidesync-shots"))
    parser.add_argument("--sizes", default=",".join(SIZES))
    args = parser.parse_args()
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    h = Harness()
    report = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            for size in args.sizes.split(","):
                h.post("stop")
                h.post("__harness/scenario", {"library": "eta"})
                context = browser.new_context(**SIZES[size])
                page = context.new_page()
                page.set_default_timeout(6000)
                run = Run(h, page, size, shots)
                page.goto(h.base)
                run.step("library states", run.library_states)
                run.step("start", run.start)
                run.step("vibe chip", run.vibe_chip)
                run.step("calmer/hotter", run.energy)
                run.step("block + undo", run.block_undo)
                run.step("edit listener", run.edit_listener)
                run.step("search row = play next", run.search_play_next)
                run.step("settings", run.settings)
                run.step("stop", run.stop)
                report[size] = {"steps_ok": run.steps, "failures": run.failures,
                                "console_errors": run.errors, "audits": run.audits}
                context.close()
            browser.close()
    finally:
        h.close()
    bad = 0
    for size, r in report.items():
        print(f"== {size}: {len(r['steps_ok'])} flows ok; failures: {len(r['failures'])}; "
              f"console errors: {len(r['console_errors'])}")
        for f in r["failures"] + r["console_errors"]:
            print("   !", f)
        for state, a in r["audits"].items():
            issues = a["small"] + a["smallFields"] + a["overflow"] + a["smallText"]
            bad += len(issues) + (1 if a["prompts"] else 0)
            print(f"   {state:16} targets={a['targets']:3} small={len(a['small'])} fields={a['fields']} "
                  f"smallFields={len(a['smallFields'])} overflow={len(a['overflow'])} "
                  f"minFont={a['minFont']}px smallText={len(a['smallText'])} dialogs={a['prompts']}")
            for issue in issues[:8]:
                print("      -", issue)
        bad += len(r["failures"]) + len(r["console_errors"])
    print("RESULT:", "PASS" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
