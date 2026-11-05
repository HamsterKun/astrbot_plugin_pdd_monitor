// 可视化登录：把服务器上那台浏览器的画面拉下来，再把操作送回去。
//
// 没有任何授权逻辑——这一页本身就挂在 AstrBot 面板里，能打开就说明已经登录过
// 面板了；后端路由也只在面板的登录态下可用。
//
// 状态机（和 login_web.RemoteLogin.status 对齐）：
//   没有会话 → 点「开始登录」→ starting/ready（一直拉帧）
//   → success（停帧，盖成功浮层）／error（停帧，盖失败浮层）／closed（收画面）

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const img = document.getElementById('screen');
const stateEl = document.getElementById('state');
const stateText = document.getElementById('state-text');
const hint = document.getElementById('hint');
const stage = document.getElementById('stage');
const overlay = document.getElementById('overlay');
const ovCard = document.getElementById('ov-card');
const ovMark = document.getElementById('ov-mark');
const ovTitle = document.getElementById('ov-title');
const ovDesc = document.getElementById('ov-desc');
const ovAgain = document.getElementById('ov-again');
const ovDismiss = document.getElementById('ov-dismiss');
const textInput = document.getElementById('text');
const btnStart = document.getElementById('btn-start');
const btnReload = document.getElementById('btn-reload');
const btnClose = document.getElementById('btn-close');
const btnType = document.getElementById('btn-type');

const FRAME_MS = 400;
const LOST_LIMIT = 4;

let vw = 900;
let vh = 720;
let lastFrame = '';
let fetching = false;
let finished = false;   // 会话已经出结果（成功/失败/结束），不再拉帧
let lost = 0;           // 连续取帧失败次数
let okMsg = '';         // 成功卡片的正文，watchClose 改尾句时要用
let timer = 0;

// ------------------------------------------------------------------ 界面状态
function setChip(kind, text) {
  stateEl.dataset.kind = kind;
  stateText.textContent = text;
  stateEl.title = text;
}

function setButtons(canStart, canTouch) {
  btnStart.disabled = !canStart;
  btnReload.disabled = !canTouch;
  btnClose.disabled = !canTouch;
  textInput.disabled = !canTouch;
  btnType.disabled = !canTouch;
}

function showOverlay(kind, title, desc, canAgain) {
  ovCard.dataset.kind = kind;
  ovMark.textContent = kind === 'err' ? '✕' : '✓';
  ovTitle.textContent = title;
  ovDesc.textContent = desc || '';
  ovAgain.hidden = !canAgain;
  overlay.hidden = false;
}

function hideOverlay() {
  overlay.hidden = true;
  ovDesc.textContent = '';
}

function showHint(text) {
  hint.innerHTML = text;
  hint.style.display = 'block';
  img.style.display = 'none';
  lastFrame = '';
}

function showFrame(b64) {
  if (b64 === lastFrame) return;
  lastFrame = b64;
  img.src = `data:image/jpeg;base64,${b64}`;
  img.style.display = 'block';
  hint.style.display = 'none';
}

// 三种收尾：成功 / 失败 / 干净结束
function finishOk(message, closed) {
  finished = true;
  setButtons(true, false);
  setChip('ok', `登录成功`);
  okMsg = message || 'cookie 已写入配置';
  const tail = closed
    ? '服务器上的浏览器已经关了，这一页可以关掉了。'
    : '服务器上的浏览器正在关（几秒后自动退），这一页可以关掉了。';
  showOverlay('ok', '登录成功', `${okMsg}\n${tail}`, true);
  if (!closed) watchClose();
}

// 成功后几秒内浏览器才会真的退出，回来把那张卡片的尾句改掉，
// 免得用户盯着「正在关」猜它到底关没关。
async function watchClose() {
  for (let i = 0; i < 4; i += 1) {
    await new Promise((r) => setTimeout(r, 1500));
    if (overlay.hidden || ovCard.dataset.kind !== 'ok') return;
    try {
      const s = await bridge.apiGet('login/state');
      if (!s || s.closed === true || s.status === 'idle' || s.status === 'closed') {
        ovDesc.textContent = `${okMsg}\n服务器上的浏览器已经关了，这一页可以关掉了。`;
        return;
      }
    } catch (_) {
      return;
    }
  }
}

function finishErr(message) {
  finished = true;
  setButtons(true, false);
  setChip('err', '登录失败');
  showOverlay('err', '登录没成功', message || '看日志里 [pdd] 开头的几行', true);
}

function finishIdle() {
  finished = true;
  setButtons(true, false);
  setChip('idle', '会话已结束');
  hideOverlay();
  showHint('会话已经结束，服务器上的浏览器也关了。<br />要再登一次就点「开始登录」。');
}

// 画布坐标 → 页面 CSS 坐标。图片被 CSS 缩放过，必须按比例换算，
// 否则点「确认」会点到旁边去（滑块更难对准）。
function toPage(e) {
  const r = img.getBoundingClientRect();
  if (!r.width || !r.height) return { x: 0, y: 0 };
  const clamp = (v, max) => Math.max(0, Math.min(max, Math.round(v)));
  return {
    x: clamp(((e.clientX - r.left) / r.width) * vw, vw - 1),
    y: clamp(((e.clientY - r.top) / r.height) * vh, vh - 1),
  };
}

async function send(payload) {
  try {
    return await bridge.apiPost('login/input', payload);
  } catch (err) {
    if (!finished) setChip('warn', `操作没送出去：${err.message || err}`);
    return null;
  }
}

// -------------------------------------------------------------------- 拉帧
async function tick() {
  if (fetching || finished || document.hidden) return;
  fetching = true;
  try {
    const r = await bridge.apiGet('login/frame');
    lost = 0;
    if (!r) return;
    // 后端在会话结束后仍然回快照（没有 img），据此切到收尾界面
    if (r.status === 'success') {
      finishOk(r.message, r.closed === true);
      return;
    }
    if (r.status === 'error') {
      finishErr(r.message);
      return;
    }
    if (r.status === 'closed') {
      finishIdle();
      return;
    }
    if (r.img) {
      vw = r.w || vw;
      vh = r.h || vh;
      showFrame(r.img);
      const left = r.left > 0 ? ` · 剩余 ${Math.ceil(r.left / 60)} 分钟` : '';
      setChip('live', `${r.message || '登录中'}${left}`);
    }
  } catch (err) {
    const msg = String(err && (err.message || err));
    if (msg.includes('没有进行中')) {
      // 会话被别处关掉了，或者插件重载过
      finishIdle();
      return;
    }
    lost += 1;
    if (lost >= LOST_LIMIT && !finished) {
      setChip('warn', '画面卡住了，点「刷新页面」试试');
    }
  } finally {
    fetching = false;
  }
}

// 页面切到后台就停帧，省服务器那台浏览器的截图开销
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !finished) tick();
});

// -------------------------------------------------------------------- 交互
img.addEventListener('pointerdown', async (e) => {
  e.preventDefault();
  try {
    img.setPointerCapture(e.pointerId);
  } catch (_) {}
  await send({ t: 'down', ...toPage(e) });
});

let lastMove = 0;
img.addEventListener('pointermove', (e) => {
  if (!e.buttons) return;
  const now = performance.now();
  if (now - lastMove < 50) return;
  lastMove = now;
  send({ t: 'move', ...toPage(e) });
});

img.addEventListener('pointerup', async (e) => {
  await send({ t: 'up', ...toPage(e) });
});

img.addEventListener('pointercancel', async () => {
  await send({ t: 'up' });
});

img.addEventListener(
  'wheel',
  (e) => {
    e.preventDefault();
    send({ t: 'wheel', dy: e.deltaY });
  },
  { passive: false },
);

// ------------------------------------------------------------------ 顶部按钮
async function startLogin() {
  hideOverlay();
  finished = false;
  lost = 0;
  if (!timer) timer = setInterval(tick, FRAME_MS);
  setChip('live', '正在打开登录页…');
  setButtons(false, false);
  try {
    const r = await bridge.apiPost('login/start', {});
    if (r && r.message) setChip('live', r.message);
    setButtons(false, true);
  } catch (err) {
    finished = true;
    setButtons(true, false);
    setChip('err', '打不开登录页');
    showOverlay('err', '打不开登录页', String(err.message || err), true);
    return;
  }
  tick();
}

btnStart.addEventListener('click', startLogin);

btnReload.addEventListener('click', async () => {
  lost = 0;
  setChip('live', '正在刷新页面…');
  await send({ t: 'reload' });
});

btnClose.addEventListener('click', async () => {
  try {
    await bridge.apiPost('login/close', {});
  } catch (_) {}
  finishIdle();
});

btnType.addEventListener('click', async () => {
  const text = textInput.value;
  if (!text) return;
  await send({ t: 'type', text });
  textInput.value = '';
});

textInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    btnType.click();
  }
});

// ------------------------------------------------------------------ 浮层按钮
ovAgain.addEventListener('click', () => {
  startLogin();
});

ovDismiss.addEventListener('click', () => {
  hideOverlay();
  // 成功后那张最后画面已经不会再更新了，留着只会让人以为卡住
  if (finished) showHint('这次登录结束了。<br />要再登一次就点「开始登录」。');
});

// ------------------------------------------------------------------ 启动
try {
  await bridge.ready();
} catch (_) {}

setButtons(true, false);
timer = setInterval(tick, FRAME_MS);
tick();
