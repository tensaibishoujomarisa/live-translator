// UI 渲染验收：在真实的 Chromium 里加载字幕窗，注入合成事件，然后断言 DOM。
//
// 为什么需要它：Electron 冒烟测试只能证明「窗口开了、后端 ready」，
// 不能证明「partial/final/translation 事件真的变成了屏幕上的字幕行」。
//
//   electron . --ui-test
'use strict';

const { app, BrowserWindow } = require('electron');
const path = require('path');

let pass = 0;
let fail = 0;
const failures = [];

function check(name, cond, detail) {
  if (cond) { pass++; console.log(`  \u2714 ${name}`); }
  else { fail++; failures.push(name); console.log(`  \u2718 ${name}${detail ? '  (' + detail + ')' : ''}`); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 必须注入 style.css：滚动、可见行数、鼠标穿透这些行为都依赖真实 CSS 求值，
// 光有 DOM 是测不出来的（没有样式表时 getComputedStyle 全是不准的默认值）。
const CSS = require('fs').readFileSync(
  path.join(__dirname, 'renderer', 'style.css'), 'utf8');

async function run() {
  // 窗口开高一点，才能测「往上滚看历史」
  const win = new BrowserWindow({
    width: 1000,
    height: 420,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'ui-test-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false
    }
  });

  win.webContents.on('console-message', (_e, level, message, line, source) => {
    if (level >= 2) console.log(`  [page] ${message} (${source}:${line})`);
  });

  await win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  await win.webContents.insertCSS(CSS);
  await sleep(400);

  const js = (code) => win.webContents.executeJavaScript(code, true);
  const emit = (msg) => js(`window.__uiTest.emit(${JSON.stringify(msg)})`);

  console.log('='.repeat(66));
  console.log(' 字幕窗 DOM 渲染验收');
  console.log('='.repeat(66));

  const listeners = await js('window.__uiTest.listenerCount()');
  check('renderer 已订阅事件', listeners >= 1, `${listeners} 个监听器`);

  // ---------- 字幕渲染 ----------
  console.log('\n[字幕渲染]');
  await emit({ kind: 'status', stage: 'running', message: '正在监听系统声音' });
  await emit({ kind: 'partial', id: 2, text: '盛永のおいしい牛乳は' });
  await sleep(80);
  const rows1 = await js(`document.querySelectorAll('#subs .line').length`);

  // 同一句的后续 partial 必须覆盖同一行，而不是不停堆新行
  await emit({ kind: 'partial', id: 2, text: '盛永のおいしい牛乳は濃い青色に' });
  await sleep(80);
  const rows2 = await js(`document.querySelectorAll('#subs .line').length`);
  check('partial 不堆叠（同一句覆盖同一行）', rows1 === 1 && rows2 === 1, `1st=${rows1} 2nd=${rows2}`);

  const jaPartial = await js(`document.querySelector('#subs .line .ja').textContent`);
  check('partial 日文可见并更新', jaPartial === '盛永のおいしい牛乳は濃い青色に', String(jaPartial));
  const isPartialClass = await js(`document.querySelector('#subs .line').classList.contains('partial')`);
  check('未定稿行带 partial 样式', isPartialClass === true);

  await emit({
    kind: 'final', id: 2, compute_ms: 210, rtf: 0.033,
    text: '森永のおいしい牛乳は濃い青色に牛乳瓶をあしらったデザインのパック牛乳である。'
  });
  await sleep(80);
  await emit({
    kind: 'translation', id: 2, latency_ms: 196,
    text: '森永的美味牛乳是深蓝色装有牛奶瓶的包装牛奶。',
    source: '森永のおいしい牛乳は…'
  });
  await sleep(200);

  const st = await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    const last = r[r.length - 1];
    const zh = last.querySelector('.zh');
    const ja = last.querySelector('.ja');
    return {
      rows: r.length,
      ja: ja ? ja.textContent : null,
      zh: zh ? zh.textContent : null,
      pending: zh ? zh.classList.contains('pending') : null,
      partial: last.classList.contains('partial'),
      meta: last.querySelector('.meta') ? last.querySelector('.meta').textContent : '',
      zhSize: zh ? getComputedStyle(zh).fontSize : null,
      jaDisplay: ja ? getComputedStyle(ja).display : null,
      hintGone: !document.querySelector('#hint'),
      status: document.querySelector('#status').textContent,
      dot: document.querySelector('#dot').className
    };
  })()`);

  check('定稿行不再是 partial 样式', st.partial === false);
  check('日文原文上屏', st.ja === '森永のおいしい牛乳は濃い青色に牛乳瓶をあしらったデザインのパック牛乳である。', String(st.ja));
  check('中文译文上屏', st.zh === '森永的美味牛乳是深蓝色装有牛奶瓶的包装牛奶。', String(st.zh));
  check('译文到达后移除 pending 动画', st.pending === false, String(st.pending));
  check('显示识别与翻译耗时', st.meta.includes('210') && st.meta.includes('196'), String(st.meta));
  check('中文字号来自设置(30px)', st.zhSize === '30px', String(st.zhSize));
  check('提示文案已移除', st.hintGone === true);
  check('状态栏显示后端消息', st.status === '正在监听系统声音', String(st.status));
  check('监听时指示灯为 listen', String(st.dot).includes('listen'), String(st.dot));

  // ---------- 一段日文被切成多句翻译（真实踩过的 bug） ----------
  console.log('\n[多句译文不互相覆盖]');
  // 用户实际遇到的情况：一句话里有多个句子，后端逐句送翻译，每条都带同一个 id
  await emit({
    kind: 'final', id: 77, compute_ms: 300,
    text: 'クレヨンしんちゃんは何？あ、子供時代の思い出みたいな感じだった。'
  });
  await sleep(60);
  await emit({ kind: 'translation', id: 77, part: 1, latency_ms: 300, text: '蜡笔小新是什么？' });
  await sleep(80);
  const afterPart1 = await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    const last = r[r.length - 1];
    return { zh: last.querySelector('.zh').textContent, pending: last.querySelector('.zh').classList.contains('pending') };
  })()`);
  check('第 1 句译文先上屏', afterPart1.zh === '蜡笔小新是什么？', String(afterPart1.zh));

  // 后端从第二句起发「累积文本」
  await emit({
    kind: 'translation', id: 77, part: 2, latency_ms: 280,
    text: '蜡笔小新是什么？啊，就是那种童年回忆的感觉。'
  });
  await sleep(150);
  const afterPart2 = await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    const last = r[r.length - 1];
    const ja = last.querySelector('.ja').textContent;
    return {
      zh: last.querySelector('.zh').textContent,
      ja,
      meta: last.querySelector('.meta') ? last.querySelector('.meta').textContent : '',
      rows: r.length
    };
  })()`);
  check('第 2 句累积成完整译文（不再覆盖）',
    afterPart2.zh === '蜡笔小新是什么？啊，就是那种童年回忆的感觉。', String(afterPart2.zh));
  check('日文原文保持完整', afterPart2.ja === 'クレヨンしんちゃんは何？あ、子供時代の思い出みたいな感じだった。', String(afterPart2.ja));
  check('标注了句数', afterPart2.meta.includes('2 句'), String(afterPart2.meta));

  // 兼容后端不累积的情况（发来的是独立的一句，前端也要拼上而不是覆盖）
  await emit({ kind: 'final', id: 78, compute_ms: 200, text: 'テスト一文目。テスト二文目' });
  await sleep(60);
  await emit({ kind: 'translation', id: 78, latency_ms: 200, text: '测试第一句。' });
  await sleep(80);
  await emit({ kind: 'translation', id: 78, latency_ms: 200, text: '测试第二句' });
  await sleep(150);
  const appended = await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    return r[r.length - 1].querySelector('.zh').textContent;
  })()`);
  check('后端不累积时前端自己拼接', appended === '测试第一句。测试第二句', String(appended));

  // ---------- 越用越准：悬停修正 ----------
  console.log('\n[越用越准]');
  await emit({
    kind: 'final', id: 500, compute_ms: 200,
    text: 'ぺこらはかわいい'
  });
  await sleep(60);
  await emit({ kind: 'translation', id: 500, latency_ms: 200, text: '佩科拉很可爱' });
  await sleep(200);

  const fixBtn = await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    const last = r[r.length - 1];
    const b = last.querySelector('.fix-btn');
    return { exists: !!b, css: getComputedStyle(last).position };
  })()`);
  check('每行都有修正按钮', fixBtn.exists === true);
  check('行定位为 relative（按钮才能贴右上角）', fixBtn.css === 'relative', fixBtn.css);

  const popHiddenWhenClosed = await js(
    `document.querySelector('#fix-pop').classList.contains('hidden')`);
  check('修正弹层默认隐藏', popHiddenWhenClosed === true);

  // 点 ✏ 打开
  await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    r[r.length - 1].querySelector('.fix-btn').click();
  })()`);
  await sleep(250);
  const pop = await js(`(() => ({
    hidden: document.querySelector('#fix-pop').classList.contains('hidden'),
    ja: document.querySelector('#fix-ja').textContent,
    wrong: document.querySelector('#fix-wrong').textContent,
    val: document.querySelector('#fix-right').value
  }))()`);
  check('点 ✏ 打开弹层', pop.hidden === false);
  check('弹层显示日语原文', pop.ja === 'ぺこらはかわいい', pop.ja);
  check('弹层显示原来的错译', pop.wrong === '佩科拉很可爱', pop.wrong);
  check('输入框预填当前译文', pop.val === '佩科拉很可爱', pop.val);

  // 改成正确译文并保存
  await js(`(async () => {
    const inp = document.querySelector('#fix-right');
    inp.value = '佩克拉很可爱';
    document.querySelector('#fix-as-term').checked = true;
    document.querySelector('#fix-save').click();
    await new Promise(r => setTimeout(r, 400));
  })()`);
  await sleep(400);
  const afterSave = await js(`(() => {
    const r = document.querySelectorAll('#subs .line');
    const last = r[r.length - 1];
    return {
      zh: last.querySelector('.zh').textContent,
      note: document.querySelector('#fix-note').textContent
    };
  })()`);
  check('界面立刻显示改后的译文', afterSave.zh === '佩克拉很可爱', afterSave.zh);
  check('给出已记住的反馈', /已记住/.test(afterSave.note || ''), String(afterSave.note));

  const sent = await js(`window.__uiTest.corrections()`);
  check('修正已发给后端', Array.isArray(sent) && sent.length >= 1, JSON.stringify(sent));
  if (Array.isArray(sent) && sent.length) {
    const c = sent[sent.length - 1];
    check('修正内容正确', c.ja === 'ぺこらはかわいい' && c.right === '佩克拉很可爱',
      JSON.stringify(c));
    check('勾了「专有名词」会标记为 term', c.asTerm === true, String(c.asTerm));
  }

  // 空内容不提交。
  // 这里刻意在「保存成功」之后立刻操作 —— 正好验证产品侧的一个健壮性问题：
  // 保存后 0.9 秒的自动关闭定时器不能把下一次操作吃掉。
  const diag = await js(`(async () => {
    const r = document.querySelectorAll('#subs .line');
    const last = r[r.length - 1];
    const b = last.querySelector('.fix-btn');
    const before = document.querySelector('#fix-pop').classList.contains('hidden');
    if (b) b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 300));
    const pop = document.querySelector('#fix-pop');
    return {
      hasBtn: !!b,
      alreadyOpen: before === false,
      openNow: !pop.classList.contains('hidden'),
      ja: document.querySelector('#fix-ja').textContent
    };
  })()`);
  check('保存后立刻再点 ✏ 仍能打开（旧关闭定时器被作废）',
    diag.openNow === true, JSON.stringify(diag));
  check('重开后显示的是当前这条', diag.ja === 'ぺこらはかわいい', String(diag.ja));

  const empty = await js(`(async () => {
    document.querySelector('#fix-right').value = '';
    document.querySelector('#fix-save').dispatchEvent(
      new MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 300));
    return {
      note: document.querySelector('#fix-note').textContent,
      hidden: document.querySelector('#fix-pop').classList.contains('hidden')
    };
  })()`);
  check('空译文不提交并提示', /请填写/.test(empty.note || ''),
    `note=${JSON.stringify(empty.note)}`);
  check('空译文时弹层不关闭', empty.hidden === false, String(empty.hidden));

  // 取消
  await js(`document.querySelector('#fix-cancel').dispatchEvent(
    new MouseEvent('click', { bubbles: true }))`);
  await sleep(200);
  const closed = await js(
    `document.querySelector('#fix-pop').classList.contains('hidden')`);
  check('取消后弹层关闭', closed === true);

  // 统计事件
  await emit({ kind: 'learn_stats', stats: { total: 7, enabled: true, suggest_terms: 2,
    suggestions: [{ ja: 'ぺこら', right: '佩克拉' }, { ja: 'みこ', right: '小巫女' }] } });
  await sleep(200);
  const learnNote = await js(`document.querySelector('#learn-note').textContent`);
  check('设置里显示已记住条数', /7 条/.test(learnNote || ''), String(learnNote));
  check('提示术语表建议', /2 条建议/.test(learnNote || ''), String(learnNote));

  // 写进术语表按钮
  const btnPromote = await js(`(() => {
    const b = document.querySelector('#btn-promote');
    return { exists: !!b, text: b ? b.textContent.trim() : '' };
  })()`);
  check('有「写进术语表」按钮', btnPromote.exists === true);
  await js(`document.querySelector('#btn-promote').dispatchEvent(
    new MouseEvent('click', { bubbles: true }))`);
  await sleep(250);
  const promo = await js(`window.__uiTest.promotions()`);
  check('点了会通知后端提升术语', Array.isArray(promo) && promo.length >= 1,
    JSON.stringify(promo));

  // 写入结果事件
  await emit({ kind: 'terms_promoted', added: 2, updated: 1, skipped: 0, total: 40 });
  await sleep(200);
  const promoNote = await js(`document.querySelector('#learn-note').textContent`);
  check('显示写入结果', /新增 2 条.*更新 1 条.*共 40 条/.test(promoNote || ''),
    String(promoNote));

  // 没有可写的条目
  await emit({ kind: 'terms_promoted', added: 0, updated: 0, skipped: 3, total: 40 });
  await sleep(200);
  const promoNote2 = await js(`document.querySelector('#learn-note').textContent`);
  check('没有可写条目时如实提示', /没有需要写入/.test(promoNote2 || ''),
    String(promoNote2));

  // ---------- 多行滚动 + 历史回看 ----------
  console.log('\n[多行滚动与历史回看]');
  // 连发 12 句：DOM 里应保留全部（historyLines=300），但窗口只能显示约 3 行
  for (let i = 10; i < 22; i++) {
    await emit({ kind: 'final', id: i, text: 'テスト' + i + '句目', compute_ms: 100 });
    await emit({ kind: 'translation', id: i, text: '测试第' + i + '句', latency_ms: 120 });
  }
  await sleep(300);

  const scroll = await js(`(() => {
    const s = document.querySelector('#subs');
    const r = document.querySelectorAll('#subs .line');
    return {
      rows: r.length,
      last: r.length ? r[r.length-1].querySelector('.zh').textContent : null,
      scrollHeight: s.scrollHeight,
      clientHeight: s.clientHeight,
      scrollTop: s.scrollTop,
      atBottom: s.scrollHeight - s.scrollTop - s.clientHeight <= 40,
      canScroll: s.scrollHeight > s.clientHeight,
      overflowY: getComputedStyle(s).overflowY,
      hintHidden: document.querySelector('#scroll-hint').classList.contains('hidden')
    };
  })()`);
  check('DOM 里保留全部历史（不再只留可见几行）', scroll.rows >= 12, String(scroll.rows));
  check('最后一行是最新译文', scroll.last === '测试第21句', String(scroll.last));
  check('字幕区可滚动', scroll.canScroll === true, `h=${scroll.scrollHeight} c=${scroll.clientHeight}`);
  check('overflow-y 是 auto（可滚）', scroll.overflowY === 'auto', scroll.overflowY);
  check('默认跟随最新（滚到底部）', scroll.atBottom === true, `top=${scroll.scrollTop}`);
  check('跟随时不显示历史提示', scroll.hintHidden === true);

  // 往上滚 -> 应该停止跟随，并浮出「↓ 最新」与历史提示
  // 注意滚到「中间」而不是最顶端：滚到 0 时确实已无更早内容，
  // 提示会是「已到最早」（那是正确行为，测不到条数提示）
  const scrolled = await js(`(async () => {
    const s = document.querySelector('#subs');
    s.scrollTop = Math.max(0, Math.round(s.scrollHeight * 0.35));
    s.dispatchEvent(new Event('scroll'));
    await new Promise(r => setTimeout(r, 120));
    return {
      scrollTop: s.scrollTop,
      jumpHidden: document.querySelector('#btn-bottom').classList.contains('hidden'),
      hintHidden: document.querySelector('#scroll-hint').classList.contains('hidden'),
      hintText: document.querySelector('#scroll-hint').textContent
    };
  })()`);
  check('往上滚后「↓ 最新」按钮出现', scrolled.jumpHidden === false);
  check('往上滚后显示更早条数提示',
    scrolled.hintHidden === false && /还有\s*\d+\s*条更早/.test(scrolled.hintText || ''),
    String(scrolled.hintText));

  // 关键：滚动期间来了新字幕，**不能**把用户拽回底部
  await emit({ kind: 'final', id: 99, text: '新しい字幕', compute_ms: 100 });
  await emit({ kind: 'translation', id: 99, text: '新来的字幕', latency_ms: 100 });
  await sleep(250);
  const held = await js(`(() => {
    const s = document.querySelector('#subs');
    return { scrollTop: s.scrollTop, atBottom: s.scrollHeight - s.scrollTop - s.clientHeight <= 40 };
  })()`);
  check('回看时新字幕不把视图拽回底部', held.atBottom === false, `top=${held.scrollTop}`);

  // 点「↓ 最新」回到实时，并恢复跟随
  const jumped = await js(`(async () => {
    document.querySelector('#btn-bottom').click();
    await new Promise(r => setTimeout(r, 200));
    const s = document.querySelector('#subs');
    return {
      atBottom: s.scrollHeight - s.scrollTop - s.clientHeight <= 40,
      jumpHidden: document.querySelector('#btn-bottom').classList.contains('hidden')
    };
  })()`);
  check('点「↓ 最新」回到实时', jumped.atBottom === true);
  check('回到实时后按钮收起', jumped.jumpHidden === true);

  // 恢复跟随后，新字幕应再次自动滚到底
  await emit({ kind: 'final', id: 100, text: 'また新しい', compute_ms: 100 });
  await sleep(250);
  const resumed = await js(`(() => {
    const s = document.querySelector('#subs');
    return s.scrollHeight - s.scrollTop - s.clientHeight <= 40;
  })()`);
  check('恢复跟随后新字幕自动滚到底', resumed === true);

  // ---------- 电平条 ----------
  console.log('\n[电平条]');
  await emit({ kind: 'level', rms: 0.09, peak: 0.3, speaking: true });
  await sleep(150);
  const lvl = await js(`document.querySelector('#level-fill').style.width`);
  check('电平条随音量变化', !!lvl && lvl !== '0%', String(lvl));

  // ---------- 错误显示 ----------
  console.log('\n[错误显示]');
  await emit({ kind: 'error', where: 'mt', message: 'HTTP 401: 未授权' });
  await sleep(200);
  const errRows = await js(`document.querySelectorAll('#subs .line.err').length`);
  const dotAfterErr = await js(`document.querySelector('#dot').className`);
  check('错误事件渲染成错误行', errRows >= 1, String(errRows));
  check('错误后指示灯变红', String(dotAfterErr).includes('error'), String(dotAfterErr));

  // ---------- 设置面板 ----------
  console.log('\n[设置面板]');
  await js(`document.querySelector('#btn-set').click()`);
  await sleep(600);
  const panel = await js(`(() => ({
    visible: !document.querySelector('#panel').classList.contains('hidden'),
    deviceOptions: document.querySelector('#set-audioDevice').options.length,
    note: document.querySelector('#dev-note').textContent,
    modelValue: document.querySelector('#set-asrModel').value,
    providerValue: document.querySelector('#set-provider').value,
    providerOptions: document.querySelector('#set-provider').options.length,
    hasVadSlider: !!document.querySelector('#set-vadThreshold'),
    vadLabel: document.querySelector('#v-vth') ? document.querySelector('#v-vth').textContent : null,
    runningInfo: document.querySelector('#running-info').textContent
  }))()`);
  check('点击齿轮显示设置面板', panel.visible === true);
  check('识别模型下拉框回填设置', panel.modelValue === 'large-v3-turbo', panel.modelValue);
  check('翻译方式下拉框回填设置', panel.providerValue === 'llm', panel.providerValue);
  check('翻译方式有三个选项（在线/本地/关闭）', panel.providerOptions === 3, String(panel.providerOptions));
  check('有语音判定阈值滑块', panel.hasVadSlider === true);
  check('阈值滑块显示当前值', panel.vadLabel === '0.50', String(panel.vadLabel));
  check('回环设备填入下拉框', panel.deviceOptions >= 3, `${panel.deviceOptions} 项（含占位项）`);
  check('显示采集方式说明', String(panel.note).includes('pyaudiowpatch'), String(panel.note));

  // 「当前实际在跑什么」必须能反映后端状态，用户靠它确认切换生效了
  await emit({ kind: 'status', stage: 'mt', message: '翻译后端已切换：本地NMT(MarianMT, cpu)' });
  await emit({
    kind: 'ready', asr: 'large-v3-turbo', device: 'cuda',
    translator: '本地NMT(MarianMT, cpu)',
    audio: { name: '耳机 (HyperX Cloud III) [Loopback]', samplerate: 48000 }
  });
  await sleep(200);
  const info = await js(`document.querySelector('#running-info').textContent`);
  check('面板显示当前翻译后端', String(info).includes('MarianMT'), String(info));
  check('面板显示当前识别设备', String(info).includes('large-v3-turbo'), String(info));
  check('面板显示当前音源', String(info).includes('HyperX'), String(info));

  // 关掉双语（取最后一个「有日文行」的字幕，错误行没有 .ja）
  const jaHidden = await js(`(async () => {
    const cb = document.querySelector('#set-showSource');
    cb.checked = false;
    cb.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 100));
    const rows = Array.from(document.querySelectorAll('#subs .line')).filter(l => l.querySelector('.ja'));
    if (!rows.length) return 'no-rows';
    const ja = rows[rows.length - 1].querySelector('.ja');
    return getComputedStyle(ja).display === 'none';
  })()`);
  check('关闭双语后日文隐藏', jaHidden === true, String(jaHidden));

  // ---------- 清屏 ----------
  console.log('\n[清屏]');
  await js(`document.querySelector('#btn-clear').click()`);
  await sleep(200);
  const afterClear = await js(`document.querySelectorAll('#subs .line').length`);
  check('点清屏后字幕清空', afterClear === 0, String(afterClear));

  console.log('\n' + '='.repeat(66));
  console.log(` 通过 ${pass} 项，失败 ${fail} 项`);
  if (fail) console.log(' 失败项：' + failures.join('、'));
  console.log('='.repeat(66));

  win.destroy();
  app.exit(fail === 0 ? 0 : 1);
}

app.whenReady().then(() => {
  run().catch((e) => {
    console.error('UI 测试异常：', e);
    app.exit(1);
  });
});

app.on('window-all-closed', () => app.exit(fail === 0 ? 0 : 1));
