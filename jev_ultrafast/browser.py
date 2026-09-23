"""通过 Browser Harness 观察和执行页面动作，并按 frame 路由 DOM 操作。"""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# 每个 execution context 都运行同一份单文档快照；Python 负责合并 frame。
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


class StalePage(ValueError):
    """决策不再对应当前页面。"""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self._frames = {}
        self._frame_order = []
        self._after_input = None
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("DOM.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780,
                  deviceScaleFactor=1, mobile=False)
        # 保持后台 tab 的渲染，不激活用户当前可见的 Chrome tab。
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def _flatten_frame_tree(self, node, parent_id=None, path=()):
        frame = node["frame"]
        record = {
            "frame_id": frame["id"],
            "parent_id": parent_id,
            "frame_path": list(path),
            "loader_id": frame.get("loaderId"),
            "url": frame.get("url", ""),
            "name": frame.get("name", ""),
            "session": self.session,
            "context_id": None,
            "target_id": None,
            "generation": 1,
            "available": True,
        }
        yield record
        for index, child in enumerate(node.get("childFrames", [])):
            yield from self._flatten_frame_tree(child, record["frame_id"], (*path, index))

    def _find_iframe_target(self, record):
        try:
            infos = cdp("Target.getTargets").get("targetInfos", [])
        except Exception:
            return None
        candidates = [
            info for info in infos
            if info.get("type") == "iframe"
            and (info.get("url") == record["url"] or info.get("targetId") == record["frame_id"])
        ]
        if not candidates:
            candidates = [
                info for info in infos
                if info.get("type") == "iframe" and info.get("url") == record["url"]
            ]
        return candidates[0] if candidates else None

    def _detach_frame_session(self, record):
        session = record.get("session")
        if session and session != self.session:
            try:
                cdp("Target.detachFromTarget", sessionId=session)
            except Exception:
                pass

    def _create_frame_context(self, record):
        if record["parent_id"] is None:
            record["session"] = self.session
            record["context_id"] = None
            record["available"] = True
            return
        try:
            response = self.call(
                "Page.createIsolatedWorld",
                frameId=record["frame_id"],
                worldName="jev-fast",
                grantUniveralAccess=False,
            )
            record["session"] = self.session
            record["context_id"] = response["executionContextId"]
            record["available"] = True
            return
        except Exception:
            # OOPIF 不属于 root session 的 execution context，按 CDP target 附加。
            target = self._find_iframe_target(record)
            if target is None:
                record["available"] = False
                return
            child_session = cdp(
                "Target.attachToTarget", targetId=target["targetId"], flatten=True
            )["sessionId"]
            try:
                cdp("Page.enable", session_id=child_session)
                cdp("Runtime.enable", session_id=child_session)
                cdp("DOM.enable", session_id=child_session)
                response = cdp(
                    "Page.createIsolatedWorld",
                    session_id=child_session,
                    frameId=record["frame_id"],
                    worldName="jev-fast",
                    grantUniveralAccess=False,
                )
            except Exception:
                try:
                    cdp("Target.detachFromTarget", sessionId=child_session)
                except Exception:
                    pass
                record["available"] = False
                return
            record["session"] = child_session
            record["target_id"] = target["targetId"]
            record["context_id"] = response["executionContextId"]
            record["available"] = True

    def _sync_frames(self):
        response = self.call("Page.getFrameTree")
        tree = response.get("frameTree")
        if not tree:
            raise StalePage("Frame tree is unavailable")
        records = list(self._flatten_frame_tree(tree))
        old = self._frames
        new = {}
        for record in records:
            previous = old.get(record["frame_id"])
            if previous and previous.get("loader_id") == record.get("loader_id"):
                for key in ("session", "context_id", "target_id", "generation", "available"):
                    record[key] = previous.get(key)
            elif previous:
                record["generation"] = previous.get("generation", 0) + 1
                self._detach_frame_session(previous)
            record["token"] = f"{record['frame_id']}:{record['generation']}"
            new[record["frame_id"]] = record
        for frame_id, previous in old.items():
            if frame_id not in new:
                self._detach_frame_session(previous)
        self._frames = new
        self._frame_order = [record["frame_id"] for record in records]
        for record in records:
            if record.get("context_id") is None and record["parent_id"] is not None:
                self._create_frame_context(record)
            elif record["parent_id"] is None:
                self._create_frame_context(record)
        return [self._frames[frame_id] for frame_id in self._frame_order]

    def _frame_chain(self, record):
        chain = []
        current = record
        while current is not None:
            chain.append(current)
            current = self._frames.get(current.get("parent_id"))
        return list(reversed(chain))

    def _frame_request(self, record):
        return {
            "session": record["session"],
            "context_id": record.get("context_id"),
            "frame": record,
            "root_session": self.session,
        }

    def _evaluate_frame(self, record, expression, await_promise=False):
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }
        if record.get("context_id") is not None:
            params["contextId"] = record["context_id"]
        response = cdp("Runtime.evaluate", session_id=record["session"], **params)
        if response.get("exceptionDetails"):
            raise StalePage("Frame execution context changed")
        return response.get("result", {}).get("value")

    def _wait_after_input(self, action):
        record = self._frames.get(action.get("frame_id"))
        if not record or record.get("token") != action.get("frame_token"):
            return
        expression = """(action => new Promise(resolve => {
          const field=window.__jevFast?.nodes.get(action.node);
          const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
          let frames=0, stopped=false;
          const finish=()=>{stopped=true;resolve()};
          setTimeout(finish,autocomplete ? 200 : 50);
          const ready=()=>{
            if (stopped) return;
            const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
              .split(/\\s+/).filter(Boolean);
            const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
            const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
            if (++frames>=2 && (!autocomplete || options.some(e=>{
              const r=e.getBoundingClientRect();
              return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
            }))) finish();
            else requestAnimationFrame(ready);
          };
          requestAnimationFrame(ready);
        }))(""" + json.dumps(action) + ")"
        try:
            self._evaluate_frame(record, expression, await_promise=True)
        except (RuntimeError, StalePage):
            pass

    def _reset_frame_context(self, record):
        self._detach_frame_session(record)
        record["session"] = self.session
        record["context_id"] = None
        record["target_id"] = None
        record["available"] = True
        self._create_frame_context(record)

    def _observe_frame(self, record, screenshot=False):
        request = {"operation": "observe", "screenshot": screenshot}
        request.update(self._frame_request(record))
        try:
            return browser_operation(request)
        except (StalePage, RuntimeError) as exc:
            if "context" not in str(exc).lower():
                raise
            self._reset_frame_context(record)
            request.update(self._frame_request(record))
            return browser_operation(request)

    def _frame_signature(self, records):
        return tuple(
            (record["frame_id"], record.get("parent_id"), record.get("loader_id"), record.get("url", ""))
            for record in records
        )

    def observe(self, screenshot=True):
        if self._after_input:
            action, self._after_input = self._after_input, None
            self._wait_after_input(action)
        records = self._sync_frames()
        for _ in range(5):
            time.sleep(0.02)
            latest = self._sync_frames()
            if self._frame_signature(records) == self._frame_signature(latest):
                records = latest
                break
            records = latest
        snapshots = []
        for record in records:
            if not record.get("available"):
                continue
            try:
                snapshots.append(
                    (record, self._observe_frame(
                        record,
                        screenshot=record["parent_id"] is None and screenshot,
                    ))
                )
            except StalePage:
                if record["parent_id"] is None:
                    raise
                record["available"] = False
        if not snapshots or snapshots[0][0]["parent_id"] is not None:
            raise StalePage("Root frame did not settle")

        actions = []
        next_element = 1
        omitted_actions = 0
        frame_states = {}
        frame_markers = {}
        root_record, root_snapshot = snapshots[0]
        text_parts = []
        for record, snapshot in snapshots:
            frame_id = record["frame_id"]
            frame_states[frame_id] = {
                "page_key": snapshot["page_key"],
                "marker": snapshot["marker"],
                "guards": snapshot["guards"],
                "scroll": snapshot["scroll"],
                "w": snapshot["w"],
                "h": snapshot["h"],
                "token": record["token"],
            }
            frame_markers[frame_id] = snapshot["marker"]
            if snapshot.get("text"):
                text_parts.append(snapshot["text"])
            chain = self._frame_chain(record)
            chain_data = [
                {
                    "frame_id": item["frame_id"],
                    "token": item["token"],
                    "frame_path": item["frame_path"],
                    "w": frame_states.get(item["frame_id"], {}).get("w", snapshot["w"]),
                    "h": frame_states.get(item["frame_id"], {}).get("h", snapshot["h"]),
                }
                for item in chain
            ]
            for action in snapshot["actions"]:
                if action["kind"] == "wait":
                    continue
                if len(actions) >= 250:
                    omitted_actions += 1
                    continue
                action = dict(action)
                action["frame_id"] = frame_id
                action["frame_token"] = record["token"]
                action["frame_path"] = record["frame_path"]
                action["frame_page_key"] = snapshot["page_key"]
                action["frame_guard"] = snapshot["guards"].get(str(action.get("node")))
                action["frame_chain"] = chain_data
                if action["kind"] == "scroll":
                    suffix = "root" if not record["frame_path"] else "_".join(map(str, record["frame_path"]))
                    action["id"] = f"{action['id']}_{suffix}"
                else:
                    action["id"] = f"e{next_element}"
                    next_element += 1
                actions.append(action)
            omitted_actions += snapshot.get("omitted_actions", 0)

        actions.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
        manifest = [
            {
                key: record[key]
                for key in ("frame_id", "parent_id", "frame_path", "loader_id", "url", "name", "token", "available")
            }
            for record in records
        ]
        state = {
            "url": root_snapshot["url"],
            "title": root_snapshot["title"],
            "w": root_snapshot["w"],
            "h": root_snapshot["h"],
            "text": "\n".join(text_parts)[:6000],
            "scroll": root_snapshot["scroll"],
            "actions": actions,
            "marker": {"manifest": manifest, "markers": frame_markers},
            "page_key": root_snapshot["page_key"],
            "guards": root_snapshot["guards"],
            "frame_manifest": manifest,
            "frame_markers": frame_markers,
            "frame_states": frame_states,
            "omitted_actions": omitted_actions,
        }
        if root_snapshot.get("screenshot"):
            state["screenshot"] = root_snapshot["screenshot"]
        state["fingerprint"] = fingerprint(state)
        return state

    def _frame_point(self, action, local_point):
        chain = action.get("frame_chain") or []
        if len(chain) <= 1:
            return local_point
        # DOM.getBoxModel 已返回相对顶层 viewport 的 owner quad；嵌套 frame 无需重复叠加。
        child = chain[-1]
        owner = cdp(
            "DOM.getFrameOwner",
            session_id=self.session,
            frameId=child["frame_id"],
        )
        backend_node = owner.get("backendNodeId")
        if not backend_node:
            raise StalePage("Frame owner is unavailable")
        box = cdp("DOM.getBoxModel", session_id=self.session, backendNodeId=backend_node)
        model = box.get("model", {})
        quad = model.get("content") or model.get("border")
        if not quad or len(quad) < 8:
            raise StalePage("Frame geometry is unavailable")
        q0 = (quad[0], quad[1])
        q1 = (quad[2], quad[3])
        q3 = (quad[6], quad[7])
        width = max(1, child.get("w", 1))
        height = max(1, child.get("h", 1))
        x_ratio = local_point["x"] / width
        y_ratio = local_point["y"] / height
        return {
            "x": q0[0] + (q1[0] - q0[0]) * x_ratio + (q3[0] - q0[0]) * y_ratio,
            "y": q0[1] + (q1[1] - q0[1]) * x_ratio + (q3[1] - q0[1]) * y_ratio,
        }

    def fresh(self, page, action=None):
        if "frame_manifest" not in page:
            if action is not None and action["kind"] in {"click", "select"}:
                node = action["node"]
                if type(node) is not int:
                    return False
                current = self.evaluate(
                    "(() => { const c=window.__jevFast; "
                    f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
                )
                return current == [page["page_key"], page["guards"].get(str(node))]
            return self.evaluate(MARKER) == page["marker"]

        records = self._sync_frames()
        manifest = [
            {
                key: record[key]
                for key in ("frame_id", "parent_id", "frame_path", "loader_id", "url", "name", "token", "available")
            }
            for record in records
        ]
        if manifest != page["frame_manifest"]:
            return False
        if action is None:
            for record in records:
                if not record.get("available"):
                    return False
                if self._evaluate_frame(record, MARKER) != page["frame_markers"].get(record["frame_id"]):
                    return False
            return True

        if action.get("kind") == "wait":
            return self.fresh(page)

        frame_id = action.get("frame_id")
        record = self._frames.get(frame_id)
        if not record or record.get("token") != action.get("frame_token"):
            return False
        chain = self._frame_chain(record)
        current_chain = [item["token"] for item in chain]
        expected_chain = [item["token"] for item in action.get("frame_chain", [])]
        if current_chain != expected_chain:
            return False
        if action["kind"] not in {"click", "select", "fill"}:
            return True
        node = action.get("node")
        if type(node) is not int:
            return False
        current = self._evaluate_frame(
            record,
            "(() => { const c=window.__jevFast; "
            f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()",
        )
        return current == [action.get("frame_page_key"), action.get("frame_guard")]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        record = self._frames.get(action.get("frame_id")) if action.get("frame_id") else None
        request = {"operation": "act", "action": action, "text": text}
        if record:
            request.update(self._frame_request(record))
            request["root_point"] = self._frame_point
        elif action.get("frame_id"):
            raise StalePage("Action frame is unavailable")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation(request)
        self._after_input = action if action["kind"] not in {"wait", "scroll"} else None
        return result

    def close(self):
        for record in self._frames.values():
            self._detach_frame_session(record)
        self._frames.clear()
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    for key in ("frame_manifest", "frame_markers"):
        if key in state:
            content[key] = state[key]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]
    context_id = request.get("context_id")

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression, await_promise=False):
        params = {"expression": expression, "returnByValue": True, "awaitPromise": await_promise}
        if context_id is not None:
            params["contextId"] = context_id
        result = call("Runtime.evaluate", **params)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            if context_id is not None:
                evaluate(f"window.scrollBy(0, {json.dumps(action['delta'])})")
            else:
                call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650,
                     deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                point = {"x": target["x"], "y": target["y"]}
                if request.get("root_point"):
                    point = request["root_point"](action, point)
                root_session = request.get("root_session", session)
                for event in ("mousePressed", "mouseReleased"):
                    cdp("Input.dispatchMouseEvent", session_id=root_session, type=event,
                        x=point["x"], y=point["y"], button="left", clickCount=1)
                if kind == "fill":
                    cdp("Input.dispatchKeyEvent", session_id=root_session, type="keyDown", key="a",
                        code="KeyA", modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"])
                    cdp("Input.dispatchKeyEvent", session_id=root_session, type="keyUp", key="a",
                        code="KeyA", modifiers=4 if sys.platform == "darwin" else 2)
                    cdp("Input.insertText", session_id=root_session, text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info


# 通过属性访问避免把 frame 路由字段暴露给模型。
