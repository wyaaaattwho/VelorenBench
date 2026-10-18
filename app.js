"use strict";
(() => {
  const data = window.VELOREN_DATA;
  if (!data) return;
  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const repo = "https://github.com/wyaaaattwho/VelorenBench/blob/main/";
  const text = {
    en: {
      skip: "Skip to content", navQuests: "Quests", navInterface: "Interface", navEvidence: "Evidence", navStart: "Begin",
      heroEyebrow: "OPEN WORLDS. REAL CONSEQUENCES.", heroTitle: "A world worth reasoning about.",
      heroDescription: "An open-world 3D fantasy RPG benchmark for autonomous multimodal & tool-using agents. Observe like a player. Act through the game. Prove effects in the world.",
      explore: "Enter the quest board", viewSource: "Explore the source", heroNote: "Native Veloren renderer · Not a simulated text world",
      nativeCapture: "REAL NATIVE ENVIRONMENT", statTasks: "Tasks", statTerrain: "Terrain cells", statQualified: "Qualified selections",
      statModalities: "Modalities", statHarnesses: "External harnesses in E261", statVideo: "Min encoded E261 video*",
      overviewEyebrow: "MORE THAN A TOOL CALL", overviewTitle: "From intention to world effect.",
      overviewText: "A useful agent must do more than narrate. It must find a workshop, make a meal, survive an encounter, or keep a promise to a real NPC.",
      featureWorld: "A native world", featureWorldText: "Real terrain, collision, inventories, equipment and autonomous enemies—not a textual imitation of game state.",
      featureBoundary: "A strict boundary", featureBoundaryText: "Player-legal observations and owned tools on one side. Evaluator-owned ECS evidence and attribution on the other.",
      featureHorizon: "Connected decisions", featureHorizonText: "Gather, craft, navigate, fight, heal and trade while retaining the ingredients, equipment and goals that still matter.",
      scopeNote: "125 exact engineering selections ≠ 125 tasks or model victories. T05/Image is engineering-only; 35 legacy motor combinations are unsupported. No full-catalog leaderboard is claimed.",
      questEyebrow: "THE GUILD QUEST BOARD", questTitle: "Twenty-nine ways to meet the world.",
      questDescription: "Every quest has a real native screenshot, an unchanged public goal and explicit world-effect verification. Open a scroll to inspect its initial state and oracle.",
      searchLabel: "Search quests", familyLabel: "Family", terrainLabel: "Terrain", modalityLabel: "Modality", reset: "Reset",
      imageDisclaimer: "Native environment illustrations, not success claims", noResults: "No quests match. Try another trail.",
      architectureEyebrow: "THREE WINDOWS. ONE MEDIATED WORLD.", architectureTitle: "Choose how an agent sees.",
      architectureDescription: "Observation and action profiles are explicit. An image-only agent never receives a hidden structured world-state fallback.",
      diagramAgent: "External agent", diagramPublic: "Player-legal interface", diagramTools: "Public goal · Observations · Owned actions",
      boundaryLabel: "PUBLIC / PRIVATE MEDIATION BOUNDARY", diagramNative: "Renderer · HUD · World effects",
      diagramOracle: "Private native oracle", diagramVerify: "Actor/session joins · Effects · Validity · Cleanup",
      diagramNote: "Private judge data and administrator setup are never player tools.",
      responsesText: "Standard tool loop and declared image transport", dshText: "Official external runtime with a dedicated adapter",
      gameagentText: "Asynchronous Brain + Jev executive; separately supplied",
      validationTitle: "Watch the interface meet reality.",
      validationDescription: "Six sampled tasks × two external harnesses × three modalities. Explore all 36 cells, including original failures and independent retries—not a leaderboard.",
      legendSuccess: "Native success classification", legendHorizon: "Horizon without success", legendOther: "Other / inspect details",
      validationCaption: "Select a cell to inspect original/effective evidence and video policy.", taskLabel: "Task",
      videoCaveat: "*72.014 minutes is encoded duration: 27 full live videos and 9 with labelled terminal holds, plus 6 independent retry aliases. It is not 72 minutes of active gameplay. Runtime model metadata is unknown. Original attempts are not rewritten. GameAgent required an adapter-local startup repair and reused torch 2.13.0+cu130; six initial dsh entries failed on a declaration str/Path bug.",
      showcaseTitle: "Glimpses from the journey", showcaseDescription: "Four silent 14-second real-time excerpts. All windows precede terminal holds; resolution/frame rate are reduced for the web.",
      validationSource: "Public validation snapshot ↗", mediaSource: "Media hashes & transformations ↗",
      startEyebrow: "YOUR NEXT ADVENTURE", startTitle: "Bring your own agent.",
      startDescription: "Start with the dependency-free Python core. Native execution needs the pinned build, exact legally obtained assets, a supported GPU/X11 environment and an explicitly bound runtime.",
      installTitle: "01 / Inspect the harness", copy: "Copy", installNote: "Inspection only: these commands do not obtain game assets, start a world or call a model.",
      pathTitle: "02 / Follow the source trail", buildLink: "Native build & binding", apiLink: "Agent & provider API", supportLink: "Exact support & admission",
      viewerLink: "Read-only viewer & passive MP4", cardLink: "Benchmark card & limitations",
      platformNote: "Selected native platform: Linux/aarch64. Other platforms and clean-machine reliability remain unverified. No bundled native binaries, game assets, provider access or GameAgent runtime.",
      citationEyebrow: "LEAVE A SCHOLAR’S MARK", citationTitle: "Cite the world you evaluated.", copyBibtex: "Copy BibTeX",
      citationNote: "Repository citation, not an invented paper or DOI. Record the exact code/native/profile identities and runtime configuration used.",
      footerText: "Built on the worlds of Veloren and the work of its contributors. Independent research tooling; no official Veloren endorsement.",
      mediaNotices: "Upstream media notices", questScroll: "GUILD ARCHIVE / QUEST SCROLL",
      allFamilies: "All eight families", allTerrains: "All terrains", allModalities: "All modalities", natural: "Natural", flat: "Flat",
      searchPlaceholder: "Search a task, skill, or story…", questCount: "{n} / 29 quests", openScroll: "Open scroll ↗",
      initial: "Initial conditions", challenge: "The agent’s challenge", oracle: "Native oracle verification",
      publicGoal: "Goal definition / catalog (English; navigation coordinates are fixture-generated)", source: "Authoritative scorer & fixture source ↗", mediaNote: "Real native screenshot · environment illustration, not task-success proof",
      imagePolicy: "T05 / Image: task-engineering only. No capability admission.",
      capabilities: "Required capabilities", modalClose: "Close quest detail", copied: "Copied to clipboard.", copyFailed: "Clipboard unavailable. Select and copy the code manually.",
      effective: "Effective classification", original: "Original classification", sourceCell: "Effective source cell", stopReason: "Stop reason",
      videoPolicy: "Video policy", videoLength: "Encoded / live / hold seconds", cleanup: "Cleanup complete", unknown: "unknown",
      retry: "independent retry", terminal: "terminal hold", live: "live capture", yes: "yes",
      statusSuccess: "Success", statusHorizon: "Horizon", statusOther: "Agent failure",
      modeTitleStructured: "Structured", modeTitleHybrid: "Hybrid", modeTitleImage: "Image / full native HUD",
      modeStructured: "Ordinary player-visible state and owned receipts: map, inventory, legal recipes, dialogue and controls. The private oracle is never included.",
      modeHybrid: "The same lawful structured interface, plus native game_frame images on demand. Structured observations remain explicit rather than covert.",
      modeImage: "Native renderer PNGs, the public goal, interface/control metadata and owned receipts—not structured world-state observations. Real HUD interactions cover inventory, crafting, map, skills, trade and dialogue.",
      modeNarrow: "Explicit visual-motor-v1 is narrower: only 5 qualified selections; 35 combinations remain unsupported. T05/Image is task-engineering-only.",
      exampleLabel: "Illustrative payload shape · not a captured evaluation record"
    },
    zh: {
      skip: "跳至正文", navQuests: "任务", navInterface: "交互", navEvidence: "证据", navStart: "开始",
      heroEyebrow: "开放世界，真实结果。", heroTitle: "一个值得推理的世界。",
      heroDescription: "面向自主多模态与工具使用智能体的开放世界 3D 奇幻 RPG 基准。在玩家视野中观察，通过游戏操作行动，以世界中的真实效果验证结果。",
      explore: "走进公会任务板", viewSource: "探索开源仓库", heroNote: "Veloren 原生渲染器 · 并非文字模拟世界",
      nativeCapture: "真实原生环境", statTasks: "任务定义", statTerrain: "地形单元", statQualified: "工程合格选择",
      statModalities: "观测模态", statHarnesses: "E261 外部框架", statVideo: "分钟 E261 编码视频*",
      overviewEyebrow: "不止于一次工具调用", overviewTitle: "从行动意图，到世界效果。",
      overviewText: "有用的智能体不应只会叙述。它需要找到工坊、制作食物、在遭遇战中生存，或兑现对真实 NPC 的承诺。",
      featureWorld: "原生世界", featureWorldText: "真实地形、碰撞、背包、装备与自主敌人，而不是游戏状态的文字仿真。",
      featureBoundary: "严格边界", featureBoundaryText: "一侧是玩家合法观测与自身工具，另一侧是评测器持有的 ECS 证据与效果归因。",
      featureHorizon: "相互关联的决策", featureHorizonText: "采集、制作、导航、战斗、治疗与交易，同时记住仍需保留的原料、装备与长程目标。",
      scopeNote: "125 个精确工程资格选择 ≠ 125 个任务或模型胜利。T05/图像仅限工程模式；35 个旧版纯运动组合不支持。不宣称完整任务集的模型排行榜。",
      questEyebrow: "公会任务板", questTitle: "以二十九种方式，走进世界。",
      questDescription: "每个任务都有真实原生截图、未修改的公开目标和明确的世界效果验证。打开任务卷轴，查阅初始状态、挑战与原生判据。",
      searchLabel: "搜索任务", familyLabel: "任务族", terrainLabel: "地形", modalityLabel: "模态", reset: "重置",
      imageDisclaimer: "原生环境示意，不作为任务成功证据", noResults: "没有匹配的任务，换一条探索路径吧。",
      architectureEyebrow: "三种视野，同一受中介的世界。", architectureTitle: "选择智能体的观察方式。",
      architectureDescription: "观测与动作配置均显式声明。纯图像智能体绝不会暗中获得结构化世界状态作为回退。",
      diagramAgent: "外部智能体", diagramPublic: "玩家合法接口", diagramTools: "公开目标 · 合法观测 · 自身动作",
      boundaryLabel: "公开 / 私有中介边界", diagramNative: "渲染器 · HUD · 世界效果",
      diagramOracle: "私有原生验证器", diagramVerify: "角色/会话关联 · 效果 · 有效性 · 清理",
      diagramNote: "私有判定数据与管理员布置权限从不作为玩家工具开放。",
      responsesText: "标准工具循环与显式图像传输声明", dshText: "使用官方外部运行时的专用适配器",
      gameagentText: "异步 Brain + Jev 执行器；需单独提供",
      validationTitle: "看交互接口，如何面对现实。",
      validationDescription: "六个抽样任务 × 两种外部框架 × 三种模态。查看全部 36 个单元，保留首次失败与独立重试，而非包装成排行榜。",
      legendSuccess: "原生成功分类", legendHorizon: "时限内未成功", legendOther: "其他 / 查看详情",
      validationCaption: "选择单元，查看原始与有效记录及视频策略。", taskLabel: "任务",
      videoCaveat: "*72.014 分钟是编码时长：27 段完整实时视频，9 段包含明确标注的终帧静止延长，另有 6 个独立重试别名。这并非 72 分钟活跃游戏。实际运行模型元数据未知，原始尝试不被改写。GameAgent 使用了适配器本地启动修复并复用了 torch 2.13.0+cu130；6 次初始 dsh 运行因声明处理 str/Path 缺陷失败。",
      showcaseTitle: "旅途中的真实片段", showcaseDescription: "四段无声、实时速度的 14 秒片段。截取窗口均早于终帧延长；为网页降低了分辨率与帧率。",
      validationSource: "公开验证快照 ↗", mediaSource: "媒体哈希与转换记录 ↗",
      startEyebrow: "下一段冒险，由你开启", startTitle: "带上你的智能体。",
      startDescription: "从零运行时依赖的 Python 核心开始。原生执行需要固定版本构建、合法获取且精确匹配的资源、可用的 GPU/X11 环境，以及显式绑定的运行配置。",
      installTitle: "01 / 检查评测框架", copy: "复制", installNote: "仅用于检查：这些命令不会获取游戏资源、启动世界或调用模型。",
      pathTitle: "02 / 沿源码前行", buildLink: "原生构建与绑定", apiLink: "智能体与模型服务 API", supportLink: "精确支持范围与准入",
      viewerLink: "只读查看器与被动 MP4 录制", cardLink: "基准卡片与局限",
      platformNote: "选定原生平台：Linux/aarch64。其他平台与全新机器可靠性尚未验证。仓库不包含原生二进制、原始游戏资源、模型服务权限或 GameAgent 运行时。",
      citationEyebrow: "为研究留下准确的注脚", citationTitle: "引用你真正评测的世界。", copyBibtex: "复制 BibTeX",
      citationNote: "这是仓库引用，不虚构论文或 DOI。请记录实际使用的代码、原生构建、接口配置身份与运行参数。",
      footerText: "基于 Veloren 的世界与贡献者的工作构建。独立研究工具，不代表 Veloren 官方背书。",
      mediaNotices: "上游媒体署名", questScroll: "公会档案 / 任务卷轴",
      allFamilies: "全部八个任务族", allTerrains: "全部地形", allModalities: "全部模态", natural: "自然", flat: "平地",
      searchPlaceholder: "搜索任务、能力或故事…", questCount: "{n} / 29 个任务", openScroll: "打开卷轴 ↗",
      initial: "初始条件", challenge: "智能体需要解决的挑战", oracle: "原生验证判据",
      publicGoal: "目标定义 / 目录说明（英文；导航坐标由夹具生成）", source: "权威评分器与夹具源码 ↗", mediaNote: "真实原生截图 · 仅作环境示意，不证明任务成功",
      imagePolicy: "T05 / 图像：仅限 task-engineering，不允许 capability 评测。",
      capabilities: "必需能力", modalClose: "关闭任务详情", copied: "已复制到剪贴板。", copyFailed: "剪贴板不可用，请选中代码手动复制。",
      effective: "有效记录分类", original: "首次记录分类", sourceCell: "有效来源单元", stopReason: "终止原因",
      videoPolicy: "视频策略", videoLength: "编码 / 实时 / 静止延长（秒）", cleanup: "清理完成", unknown: "未知",
      retry: "独立重试", terminal: "终帧延长", live: "实时采集", yes: "是",
      statusSuccess: "成功", statusHorizon: "达时限", statusOther: "智能体失败",
      modeTitleStructured: "结构化观测", modeTitleHybrid: "混合观测", modeTitleImage: "图像 / 完整原生 HUD",
      modeStructured: "正常玩家可见状态与自身回执：地图、背包、合法配方、对话与控制。私有验证器信息始终不包含在内。",
      modeHybrid: "与结构化模式相同的合法接口，加上按需 game_frame 原生图像。结构化观测显式存在，不作隐蔽补充。",
      modeImage: "原生渲染 PNG、公开目标、接口/控制元数据与自身回执，不包含结构化世界状态观测。真实 HUD 交互覆盖背包、制作、地图、技能、交易及对话。",
      modeNarrow: "显式 visual-motor-v1 更受限：仅 5 个工程合格选择，35 个组合仍不支持。T05/图像仅限 task-engineering。",
      exampleLabel: "接口形状示意 · 不是实际评测记录"
    }
  };
  const capabilities = {
    dialogue: "对话", map: "地图", movement: "移动", pickup: "拾取", terrain_event: "地形事件",
    crafting: "制作", inventory: "背包", equipment: "装备", combat: "战斗", skills: "技能", trade: "交易", coordinate_goal: "坐标目标"
  };
  const modeLabels = { en: { structured: "Structured", hybrid: "Hybrid", image: "Image" }, zh: { structured: "结构化", hybrid: "混合", image: "图像" } };
  let lang = "en";
  try { lang = localStorage.getItem("veloren-language") || (navigator.language.startsWith("zh") ? "zh" : "en"); } catch { /* Private browsing still works. */ }
  if (!text[lang]) lang = "en";
  let mode = "structured";
  let selectedCell = data.validation.cells[0].id;
  let selectedQuest = null;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const t = (key) => text[lang][key] || key;
  const family = (id) => data.families.find((f) => f.id === id);
  const capability = (name) => lang === "zh" ? (capabilities[name] || name) : name.replaceAll("_", " ");
  const chips = (task) => task.capabilities.map((name) => `<span class="chip" title="${escape(name)}">${escape(capability(name))}</span>`).join("");
  const option = (value, label) => `<option value="${escape(value)}">${escape(label)}</option>`;

  function renderFilters() {
    const values = Object.fromEntries(["family", "terrain", "modality"].map((id) => [id, $("#" + id).value]));
    $("#family").innerHTML = option("", t("allFamilies")) + data.families.map((f) => option(f.id, f.icon + " " + f[lang])).join("");
    $("#terrain").innerHTML = option("", t("allTerrains")) + ["natural", "flat"].map((v) => option(v, t(v))).join("");
    $("#modality").innerHTML = option("", t("allModalities")) + Object.keys(modeLabels[lang]).map((v) => option(v, modeLabels[lang][v])).join("");
    for (const id of Object.keys(values)) $("#" + id).value = values[id];
    $("#search").placeholder = t("searchPlaceholder");
  }

  function renderQuests() {
    const query = $("#search").value.trim().toLocaleLowerCase();
    const chosenFamily = $("#family").value;
    const terrain = $("#terrain").value;
    const modality = $("#modality").value;
    const matches = data.tasks.filter((task) => {
      const searchable = [task.id, family(task.family).en, family(task.family).zh, ...task.capabilities,
        ...task.capabilities.map(capability), ...Object.values(task.en), ...Object.values(task.zh)].join(" ").toLocaleLowerCase();
      return (!chosenFamily || task.family === chosenFamily) && (!terrain || task.terrain.includes(terrain)) &&
        (!modality || task.modalities.includes(modality)) && (!query || searchable.includes(query));
    });
    $("#quest-grid").innerHTML = matches.map((task) => {
      const note = task[lang];
      return `<button type="button" class="quest-card" data-task="${escape(task.id)}" aria-label="${escape(task.id + " · " + note.title)}" aria-haspopup="dialog">
        <div class="quest-image"><img src="${escape(task.image)}" width="960" height="540" loading="lazy" decoding="async" alt="${escape(task.id + " · " + note.title + " · " + t("nativeCapture"))}">
        <span class="task-badge">${escape(task.id)}</span><span class="terrain-badge">${task.terrain.map((v) => t(v)).join(" / ")}</span></div>
        <div class="quest-card-content"><span class="quest-family">${escape(task.icon + " " + family(task.family)[lang])}</span><h3>${escape(note.title)}</h3>
        <p class="quest-summary">${escape(note.initial)}</p><div class="chips">${chips(task)}</div>
        <div class="quest-card-footer"><span>${task.id === "T05" ? (lang === "zh" ? "图像仅限工程模式" : "Image: engineering only") : "S · H · I"}</span><span>${escape(t("openScroll"))}</span></div></div></button>`;
    }).join("");
    $("#task-count").textContent = t("questCount").replace("{n}", matches.length);
    $("#no-results").hidden = matches.length > 0;
  }

  function renderQuest(task) {
    const note = task[lang];
    $("#quest-detail").innerHTML = `<img src="${escape(task.image)}" width="960" height="540" alt="${escape(task.id + " · " + note.title + " · " + t("nativeCapture"))}">
      <div class="scroll-body"><div class="scroll-meta">${escape(task.id + " / " + family(task.family)[lang])}<br>${escape(task.terrain.map((v) => t(v)).join(" / "))} · ${escape(Object.values(modeLabels[lang]).join(" / "))} (visual-ui-v1)</div>
      <h2 id="quest-dialog-title">${escape(note.title)}</h2><p>${escape(t("mediaNote"))}</p>
      ${task.id === "T05" ? `<p class="modal-policy">${escape(t("imagePolicy"))}</p>` : ""}
      <div class="chips" aria-label="${escape(t("capabilities"))}">${chips(task)}</div>
      <h3>${escape(t("initial"))}</h3><p>${escape(note.initial)}</p>
      <h3>${escape(t("challenge"))}</h3><p>${escape(note.challenge)}</p>
      <h3>${escape(t("oracle"))}</h3><div class="oracle"><p>${escape(note.oracle)}</p></div>
      <details><summary>${escape(t("publicGoal"))}</summary><p lang="en">${escape(task.publicGoal)}</p></details>
      <p><a href="${repo + escape(task.source)}">${escape(t("source"))}</a></p></div>`;
  }

  function openQuest(id, fromHash = false) {
    const task = data.tasks.find((entry) => entry.id === id);
    if (!task) return;
    selectedQuest = id;
    renderQuest(task);
    const dialog = $("#quest-dialog");
    if (!dialog.open) dialog.showModal();
    document.body.classList.add("modal-open");
    dialog.scrollTop = 0;
    if (!fromHash) history.replaceState(null, "", "#task=" + encodeURIComponent(id));
  }

  function renderModality() {
    all("[data-mode]").forEach((tab) => {
      const active = tab.dataset.mode === mode;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      tab.textContent = modeLabels[lang][tab.dataset.mode];
    });
    const suffix = mode[0].toUpperCase() + mode.slice(1);
    const snippets = {
      structured: "public_goal\nordinary player observation\n  inventory · known recipes · map\n  dialogue · owned action receipts\n→ ordinary-renderer-native-v1",
      hybrid: "public_goal + ordinary observation\n→ game_frame() when needed\n← native renderer PNG\n→ ordinary-renderer-native-v1",
      image: "public_goal + interface metadata\n← native renderer PNG\n→ HUD / motor input + control fences\n← owned action receipt\n→ visual-ui-v1"
    };
    $("#modality-panel").setAttribute("aria-labelledby", "tab-" + mode);
    $("#modality-panel").innerHTML = `<p class="eyebrow">${escape(modeLabels[lang][mode])} / OBSERVATION</p><h3>${escape(t("modeTitle" + suffix))}</h3><p>${escape(t("mode" + suffix))}</p>
      <span class="profile-tag">${mode === "image" ? "visual-ui-v1" : "ordinary-renderer-native-v1"}</span>
      ${mode === "image" ? '<img src="assets/tasks/T07.png" width="960" height="540" alt="T07 native visual HUD" loading="lazy">' : ""}
      <p class="payload-label">${escape(t("exampleLabel"))}</p><pre><code>${escape(snippets[mode])}</code></pre>${mode === "image" ? `<p class="tiny">${escape(t("modeNarrow"))}</p>` : ""}`;
  }

  function status(cell) {
    return cell.classification === "capability_success" ? "success" :
      cell.classification === "evaluation_horizon_without_success" ? "horizon" : "other";
  }
  function renderValidation() {
    $("#validation-body").innerHTML = data.validation.sampledTasks.map((task) => `<tr><th scope="row">${escape(task)}</th>${["gameagent", "dsh"].flatMap((harness) =>
      ["structured", "hybrid", "image"].map((modality) => {
        const cell = data.validation.cells.find((c) => c.task === task && c.harness === harness && c.modality === modality);
        const kind = status(cell);
        const retry = cell.sourceCell !== cell.id;
        const sub = [retry ? t("retry") : "", cell.terminalHoldSeconds > 0 ? t("terminal") : ""].filter(Boolean).join(" · ");
        const label = t(kind === "success" ? "statusSuccess" : kind === "horizon" ? "statusHorizon" : "statusOther");
        return `<td><button type="button" class="cell-button ${kind}" data-cell="${escape(cell.id)}" aria-label="${escape(cell.id + ": " + cell.classification)}" aria-pressed="${cell.id === selectedCell}">${escape(label)}<small>${escape(sub || t("live"))}</small></button></td>`;
      })).join("")}</tr>`).join("");
    renderCell();
  }
  function renderCell() {
    const cell = data.validation.cells.find((c) => c.id === selectedCell);
    all("[data-cell]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.cell === selectedCell)));
    const rows = [
      [t("effective"), cell.classification], [t("original"), cell.originalClassification],
      [t("sourceCell"), cell.sourceCell], [t("stopReason"), cell.stopReason],
      [t("videoPolicy"), cell.videoKind],
      [t("videoLength"), `${cell.videoSeconds.toFixed(3)} / ${cell.actualLiveSeconds === null ? t("unknown") : cell.actualLiveSeconds.toFixed(3)} / ${cell.terminalHoldSeconds.toFixed(3)}`],
      [t("cleanup"), cell.cleanupComplete ? t("yes") : "false"]
    ];
    $("#cell-detail").innerHTML = `<h3>${escape(cell.id)}</h3><dl>${rows.map(([label, value]) => `<dt>${escape(label)}</dt><dd>${escape(value)}</dd>`).join("")}</dl>`;
  }

  function translate() {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    document.title = lang === "zh" ? "VelorenBench · 一个值得推理的世界" : "VelorenBench · A World Worth Reasoning About";
    all("[data-i18n]").forEach((element) => { element.textContent = t(element.dataset.i18n); });
    $("#language").setAttribute("aria-label", lang === "zh" ? "Switch to English" : "切换到中文");
    $("#language").textContent = lang === "zh" ? "EN / 中文" : "中文 / EN";
    $("#close-dialog").setAttribute("aria-label", t("modalClose"));
    $("#copy-status").textContent = "";
    renderFilters(); renderQuests(); renderModality(); renderValidation();
    if (selectedQuest) renderQuest(data.tasks.find((task) => task.id === selectedQuest));
  }

  $("#language").addEventListener("click", () => {
    lang = lang === "en" ? "zh" : "en";
    try { localStorage.setItem("veloren-language", lang); } catch { /* Storage is optional. */ }
    translate();
  });
  $("#filters").addEventListener("submit", (event) => event.preventDefault());
  $("#filters").addEventListener("input", renderQuests);
  $("#filters").addEventListener("reset", () => { setTimeout(renderQuests, 0); });
  $("#quest-grid").addEventListener("click", (event) => {
    const button = event.target.closest("[data-task]");
    if (button) openQuest(button.dataset.task);
  });
  $("#quest-grid").addEventListener("pointermove", (event) => {
    if (reduced.matches || event.pointerType !== "mouse") return;
    const card = event.target.closest(".quest-card");
    if (!card) return;
    const rect = card.getBoundingClientRect();
    card.style.setProperty("--ry", `${((event.clientX - rect.left) / rect.width - .5) * 4}deg`);
    card.style.setProperty("--rx", `${-((event.clientY - rect.top) / rect.height - .5) * 3}deg`);
  });
  $("#quest-grid").addEventListener("pointerout", (event) => {
    const card = event.target.closest(".quest-card");
    if (card && !card.contains(event.relatedTarget)) { card.style.removeProperty("--ry"); card.style.removeProperty("--rx"); }
  });
  const dialog = $("#quest-dialog");
  $("#close-dialog").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
  dialog.addEventListener("close", () => {
    selectedQuest = null; document.body.classList.remove("modal-open");
    if (location.hash.startsWith("#task=")) history.replaceState(null, "", "#quests");
  });
  function readHash() {
    if (location.hash.startsWith("#task=")) {
      const id = location.hash.slice(6);
      if (data.tasks.some((task) => task.id === id)) openQuest(id, true);
    }
  }
  addEventListener("hashchange", readHash);
  all("[data-mode]").forEach((button, index, tabs) => {
    button.addEventListener("click", () => { mode = button.dataset.mode; renderModality(); });
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
        (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      mode = tabs[next].dataset.mode; renderModality(); tabs[next].focus();
    });
  });
  $("#validation-body").addEventListener("click", (event) => {
    const cell = event.target.closest("[data-cell]");
    if (cell) { selectedCell = cell.dataset.cell; renderCell(); }
  });
  all("[data-copy]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#" + button.dataset.copy).textContent);
      $("#copy-status").textContent = t("copied");
    } catch { $("#copy-status").textContent = t("copyFailed"); }
  }));

  translate(); readHash();
  if ("IntersectionObserver" in window) {
    if (!reduced.matches) document.body.classList.add("js-motion");
    const reveals = new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => { if (entry.isIntersecting) { entry.target.classList.add("visible"); observer.unobserve(entry.target); } });
    }, { threshold: .02 });
    all(".reveal").forEach((element) => reveals.observe(element));
    const counters = new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        if (reduced.matches) return;
        const target = Number(entry.target.dataset.count);
        const start = performance.now();
        function tick(now) {
          const portion = Math.min((now - start) / 1100, 1);
          entry.target.textContent = String(Math.round(target * (1 - (1 - portion) ** 3)));
          if (portion < 1) requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
      });
    }, { threshold: .3 });
    all("[data-count]").forEach((counter) => counters.observe(counter));
  }

  // Ambient mana motes: bounded draw count, capped DPR, paused in hidden tabs.
  const canvas = $("#motes"), ctx = canvas.getContext("2d");
  if (ctx) {
    let width = 0, height = 0, raf = 0, last = 0;
    const motes = Array.from({ length: 40 }, () => ({ x: Math.random(), y: Math.random(), size: .5 + Math.random() * 1.3, speed: .003 + Math.random() * .009, phase: Math.random() * 6.28 }));
    function resize() {
      width = innerWidth; height = innerHeight;
      const dpr = Math.min(devicePixelRatio || 1, 1.5);
      canvas.width = width * dpr; canvas.height = height * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function draw(now) {
      raf = 0;
      if (document.hidden || reduced.matches) return;
      const dt = Math.min((now - (last || now)) / 1000, .05); last = now;
      ctx.clearRect(0, 0, width, height);
      motes.forEach((p, index) => {
        p.y = (p.y - p.speed * dt + 1) % 1;
        const x = p.x * width + Math.sin(now / 5000 + p.phase) * 12;
        const alpha = .2 + .25 * (1 + Math.sin(now / 2500 + p.phase)) / 2;
        ctx.fillStyle = index % 4 === 0 ? `rgba(137,212,225,${alpha})` : `rgba(225,190,120,${alpha})`;
        ctx.beginPath(); ctx.arc(x, p.y * height, p.size, 0, Math.PI * 2); ctx.fill();
      });
      raf = requestAnimationFrame(draw);
    }
    function syncAnimation() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0; last = 0;
      if (!document.hidden && !reduced.matches) raf = requestAnimationFrame(draw);
      else ctx.clearRect(0, 0, width, height);
    }
    resize(); syncAnimation();
    addEventListener("resize", resize, { passive: true });
    document.addEventListener("visibilitychange", syncAnimation);
    reduced.addEventListener("change", syncAnimation);
  }
})();
