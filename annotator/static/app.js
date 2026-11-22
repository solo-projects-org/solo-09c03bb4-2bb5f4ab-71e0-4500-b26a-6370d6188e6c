/* Slice annotation hand-off UI. */
(function () {
  "use strict";

  const state = {
    revision: null,
    selectedTile: null,
    rejected: null,
  };

  const els = {
    importForm: document.getElementById("import-form"),
    importText: document.getElementById("import-text"),
    importFile: document.getElementById("import-file"),
    importResult: document.getElementById("import-result"),
    tileList: document.getElementById("tile-list"),
    emptyState: document.getElementById("empty-state"),
    viewerBody: document.getElementById("viewer-body"),
    tileTitle: document.getElementById("tile-title"),
    tileStatus: document.getElementById("tile-status"),
    slice: document.getElementById("slice"),
    confirmBtn: document.getElementById("confirm-btn"),
    confirmResult: document.getElementById("confirm-result"),
    annotationBody: document.getElementById("annotation-body"),
    conflictPanel: document.getElementById("conflict-panel"),
    driftPanel: document.getElementById("drift-panel"),
    revisionBadge: document.getElementById("revision-badge"),
    downloadLink: document.getElementById("download-link"),
    downloadPaused: document.getElementById("download-paused"),
  };

  function svgEl(name, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attrs)) {
      node.setAttribute(key, String(value));
    }
    return node;
  }

  function tone(kind) {
    return kind === "positive" ? "#3ddc84" : "#f0b429";
  }

  function loadState(payload) {
    state.revision = payload.revision || null;
    state.rejected = payload.rejected || null;
    if (!state.revision) {
      els.emptyState.classList.remove("hidden");
      els.viewerBody.classList.add("hidden");
      els.revisionBadge.textContent = "尚无生效清单";
      els.revisionBadge.className = "badge";
      els.tileList.innerHTML = "";
      els.downloadLink.classList.remove("hidden");
      els.downloadPaused.classList.add("hidden");
      renderDrift();
      renderConflicts();
      return;
    }
    els.emptyState.classList.add("hidden");
    els.viewerBody.classList.remove("hidden");
    const rev = state.revision;
    els.revisionBadge.textContent = `版本 r${rev.id} · ${rev.positive_tiles.length} 个正例切片`;
    els.revisionBadge.className = rev.drift_paused ? "badge warn" : "badge ok";
    const paused = Boolean(rev.drift_paused);
    els.downloadLink.classList.toggle("hidden", paused);
    els.downloadLink.setAttribute("aria-disabled", paused ? "true" : "false");
    els.downloadPaused.classList.toggle("hidden", !paused);
    const keep = state.selectedTile;
    const exists = rev.tiles.some((t) => t.id === keep);
    state.selectedTile = exists ? keep : (rev.tiles[0] ? rev.tiles[0].id : null);
    renderTileList();
    renderTile();
    renderDrift();
    renderConflicts();
    renderWarnings(rev);
  }

  function renderDrift() {
    const panel = els.driftPanel;
    const rev = state.revision;
    const drift = (rev && rev.drift) || [];
    if (!drift.length) {
      panel.classList.add("hidden");
      panel.innerHTML = "";
      return;
    }
    panel.classList.remove("hidden");
    panel.innerHTML = "";
    const title = document.createElement("strong");
    title.textContent =
      "检测到已确认切片错位：以下切片的现行框与首次确认的原框不一致，相关切片已暂停确认，整份清单已暂停下载。";
    panel.appendChild(title);
    const list = document.createElement("ul");
    for (const d of drift) {
      const li = document.createElement("li");
      const original = d.original_rect ? `[${d.original_rect.join(", ")}]` : "（未知）";
      const current = d.current_rect ? `[${d.current_rect.join(", ")}]` : "（不在现行版中）";
      li.textContent =
        `切片 #${d.tile}：首次确认原框 ${original} → 现行框 ${current}。`;
      list.appendChild(li);
    }
    panel.appendChild(list);
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "处理方式：重新导入包含首次确认原框的切片表并重新规划；原确认与其他切片的复核进度会保留，清单随即恢复下载。";
    panel.appendChild(hint);
  }

  function renderWarnings(rev) {
    if (!rev || !rev.warnings || !rev.warnings.length) return;
    const panel = els.conflictPanel;
    if (!panel.classList.contains("hidden")) return;
    panel.classList.remove("hidden");
    panel.classList.remove("error");
    panel.innerHTML = "<strong>提示：已确认切片的标注按原样保留，以下重叠未新增忽略区域。</strong>";
    const list = document.createElement("ul");
    for (const w of rev.warnings) {
      const li = document.createElement("li");
      li.textContent = w.message;
      list.appendChild(li);
    }
    panel.appendChild(list);
  }

  function renderConflicts() {
    const panel = els.conflictPanel;
    if (state.rejected && state.rejected.conflicts.length) {
      panel.classList.remove("hidden");
      panel.innerHTML = "";
      const title = document.createElement("div");
      title.innerHTML = `<strong>导入未生效（冲突记录 r${state.rejected.id}）。旧清单保留可用。</strong>`;
      panel.appendChild(title);
      const list = document.createElement("ul");
      for (const c of state.rejected.conflicts) {
        const li = document.createElement("li");
        li.textContent = c.message;
        list.appendChild(li);
      }
      panel.appendChild(list);
    } else {
      panel.classList.add("hidden");
      panel.innerHTML = "";
    }
  }

  function renderTileList() {
    els.tileList.innerHTML = "";
    for (const tile of state.revision.tiles) {
      const pos = tile.annotations.filter((a) => a.kind === "positive").length;
      const ign = tile.annotations.filter((a) => a.kind === "ignore").length;
      const li = document.createElement("li");
      if (tile.id === state.selectedTile) li.classList.add("active");
      li.addEventListener("click", () => {
        state.selectedTile = tile.id;
        renderTileList();
        renderTile();
      });

      const label = document.createElement("div");
      const strong = document.createElement("div");
      strong.textContent = `切片 #${tile.id}`;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `[${tile.rect.join(", ")}]`;
      label.appendChild(strong);
      label.appendChild(meta);

      const right = document.createElement("div");
      right.style.display = "flex";
      right.style.alignItems = "center";
      right.style.gap = "6px";
      const counts = document.createElement("div");
      counts.className = "counts";
      if (pos) {
        const p = document.createElement("span");
        p.className = "pill pos";
        p.textContent = `正例 ${pos}`;
        counts.appendChild(p);
      }
      if (ign) {
        const i = document.createElement("span");
        i.className = "pill ign";
        i.textContent = `忽略 ${ign}`;
        counts.appendChild(i);
      }
      if (tile.drifted) {
        const c = document.createElement("span");
        c.className = "badge warn";
        c.textContent = "已错位";
        counts.appendChild(c);
      } else if (tile.pinned) {
        const c = document.createElement("span");
        c.className = "badge ok";
        c.textContent = "已确认·冻结";
        counts.appendChild(c);
      } else if (tile.confirmed) {
        const c = document.createElement("span");
        c.className = "badge ok";
        c.textContent = "已确认";
        counts.appendChild(c);
      }
      right.appendChild(counts);
      li.appendChild(label);
      li.appendChild(right);
      els.tileList.appendChild(li);
    }
  }

  function renderTile() {
    const rev = state.revision;
    if (!rev) return;
    const tile = rev.tiles.find((t) => t.id === state.selectedTile);
    els.confirmResult.classList.add("hidden");
    els.confirmResult.textContent = "";
    if (!tile) {
      els.tileTitle.textContent = "";
      els.slice.innerHTML = "";
      return;
    }
    els.tileTitle.textContent = `切片 #${tile.id}`;
    if (tile.drifted) {
      els.tileStatus.textContent = "本切片已错位";
      els.tileStatus.className = "badge warn";
    } else if (tile.pinned) {
      els.tileStatus.textContent = "本切片确认于旧版本，标注已原样保留";
      els.tileStatus.className = "badge ok";
    } else if (tile.confirmed) {
      els.tileStatus.textContent = "本切片已确认";
      els.tileStatus.className = "badge ok";
    } else {
      els.tileStatus.textContent = "待复核";
      els.tileStatus.className = "badge";
    }

    const [tx, ty, tw, th] = tile.rect;
    els.slice.setAttribute("viewBox", `${tx} ${ty} ${tw} ${th}`);
    els.slice.setAttribute("width", tw);
    els.slice.setAttribute("height", th);
    els.slice.innerHTML = "";

    const bg = svgEl("rect", { x: tx, y: ty, width: tw, height: th, fill: "#111a2e" });
    els.slice.appendChild(bg);

    // Faint grid for pixel orientation.
    const step = Math.max(50, Math.round(Math.max(tw, th) / 8 / 50) * 50);
    for (let gx = Math.ceil(tx / step) * step; gx < tx + tw; gx += step) {
      els.slice.appendChild(svgEl("line", {
        x1: gx, y1: ty, x2: gx, y2: ty + th, stroke: "#1c2946", "stroke-width": 1,
      }));
    }
    for (let gy = Math.ceil(ty / step) * step; gy < ty + th; gy += step) {
      els.slice.appendChild(svgEl("line", {
        x1: tx, y1: gy, x2: tx + tw, y2: gy, stroke: "#1c2946", "stroke-width": 1,
      }));
    }

    const ordered = tile.annotations
      .slice()
      .sort((a, b) => (a.kind === "positive" ? 1 : 0) - (b.kind === "positive" ? 1 : 0));

    for (const ann of ordered) {
      const [x, y, w, h] = ann.rect;
      const absX = x + tx;
      const absY = y + ty;
      const rect = svgEl("rect", {
        x: absX, y: absY, width: w, height: h,
        fill: ann.kind === "positive" ? "rgba(61,220,132,.22)" : "rgba(240,180,41,.22)",
        stroke: tone(ann.kind),
        "stroke-width": 2,
        "vector-effect": "non-scaling-stroke",
      });
      const title = svgEl("title", {});
      title.textContent = `裂缝 #${ann.crack} · ${ann.kind === "positive" ? "正例" : "忽略"}`;
      rect.appendChild(title);
      els.slice.appendChild(rect);

      const text = svgEl("text", {
        x: absX + 4,
        y: absY + 16,
        fill: tone(ann.kind),
        "font-weight": 700,
      });
      text.textContent = `${ann.kind === "positive" ? "P" : "I"}${ann.crack}`;
      els.slice.appendChild(text);
    }

    els.annotationBody.innerHTML = "";
    for (const ann of tile.annotations) {
      const tr = document.createElement("tr");
      const crack = document.createElement("td");
      crack.textContent = `#${ann.crack}`;
      const kind = document.createElement("td");
      const tag = document.createElement("span");
      tag.className = `tag ${ann.kind}`;
      tag.textContent = ann.kind === "positive" ? "正例" : "忽略";
      kind.appendChild(tag);
      const rect = document.createElement("td");
      rect.className = "mono";
      rect.textContent = `[${ann.rect.join(", ")}]`;
      tr.appendChild(crack);
      tr.appendChild(kind);
      tr.appendChild(rect);
      els.annotationBody.appendChild(tr);
    }

    els.confirmBtn.disabled = tile.drifted || (tile.confirmed && !tile.pinned);
    els.confirmBtn.textContent = tile.drifted
      ? "已错位，确认暂停"
      : tile.confirmed && !tile.pinned
        ? "已确认"
        : tile.pinned
          ? "确认沿用此切片的标注"
          : "确认此切片的标注";
  }

  async function refresh(payload) {
    if (payload) {
      loadState(payload);
      return;
    }
    const res = await fetch("/api/state");
    if (res.ok) loadState(await res.json());
  }

  async function submitImport(text) {
    els.importResult.classList.remove("hidden", "error", "good");
    els.importResult.textContent = "导入中…";
    let res;
    try {
      res = await fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: text,
      });
    } catch (err) {
      els.importResult.classList.add("error");
      els.importResult.textContent = `网络错误：${err}`;
      return;
    }
    const data = await res.json().catch(() => null);
    if (!data) {
      els.importResult.classList.add("error");
      els.importResult.textContent = "服务器返回无法解析。";
      return;
    }
    if (data.ok) {
      els.importResult.classList.add("good");
      els.importResult.textContent = `导入成功：版本 r${data.revision}，${data.positive_tiles.length} 个正例切片。`;
    } else if (data.kind === "payload") {
      els.importResult.classList.add("error");
      els.importResult.textContent = "";
      const list = document.createElement("ul");
      for (const msg of data.errors) {
        const li = document.createElement("li");
        li.textContent = msg;
        list.appendChild(li);
      }
      els.importResult.textContent = "导入内容有误：";
      els.importResult.appendChild(list);
    } else {
      els.importResult.classList.add("error");
      els.importResult.textContent = "规划冲突，旧清单保持不变。详见右侧冲突面板。";
    }
    await refresh();
  }

  els.importForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitImport(els.importText.value);
  });

  els.importFile.addEventListener("change", async () => {
    const file = els.importFile.files[0];
    if (!file) return;
    els.importText.value = await file.text();
  });

  els.downloadLink.addEventListener("click", (event) => {
    if (state.revision && state.revision.drift_paused) {
      event.preventDefault();
      els.confirmResult.classList.remove("hidden", "good");
      els.confirmResult.classList.add("error");
      els.confirmResult.textContent =
        "已确认切片错位，整份清单已暂停下载；请先重新导入首次确认的原框。";
    }
  });

  els.confirmBtn.addEventListener("click", async () => {
    const tile = state.revision.tiles.find((t) => t.id === state.selectedTile);
    if (!tile) return;
    els.confirmBtn.disabled = true;
    els.confirmResult.classList.remove("hidden", "error", "good");
    els.confirmResult.textContent = "确认中…";
    let res;
    try {
      res = await fetch("/api/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tile: tile.id, token: tile.token }),
      });
    } catch (err) {
      els.confirmBtn.disabled = false;
      els.confirmResult.classList.add("error");
      els.confirmResult.textContent = `网络错误：${err}`;
      return;
    }
    const data = await res.json().catch(() => null);
    if (res.ok && data && data.ok) {
      const message = data.already
        ? "该切片此前已确认（幂等）。"
        : `已确认，记录于 ${data.confirmed_at}。`;
      await refresh();
      const stillThere = state.revision && state.revision.tiles.find((t) => t.id === tile.id);
      els.confirmResult.classList.remove("hidden");
      if (stillThere) {
        els.confirmResult.classList.add("good");
        els.confirmResult.textContent = message;
      } else {
        els.confirmResult.classList.add("error");
        els.confirmResult.textContent = "确认已记录，但现行版本中已无该切片，请复核新清单。";
      }
      return;
    }
    els.confirmBtn.disabled = false;
    els.confirmResult.classList.add("error");
    if (data && data.kind === "stale") {
      els.confirmResult.textContent = "页面显示的不是现行版本，已拒绝确认。正在刷新…";
      await refresh();
      els.confirmResult.classList.remove("hidden");
      els.confirmResult.classList.add("error");
      els.confirmResult.textContent = "页面显示的不是现行版本，已拒绝确认。视图已刷新，请复核后再确认。";
    } else if (data && data.kind === "paused") {
      els.confirmResult.textContent =
        (data.errors && data.errors.join("；")) || "该切片已错位，确认已暂停。";
      await refresh();
    } else {
      els.confirmResult.textContent = (data && data.errors && data.errors.join("；")) || "确认失败。";
    }
  });

  loadState(window.INITIAL_STATE || {});
})();
