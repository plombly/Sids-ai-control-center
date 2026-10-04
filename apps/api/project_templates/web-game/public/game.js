// {{NAME}}: a tiny canvas game to build on. Move the square, collect stars.
import { step, newGame } from './logic.js';

const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const keys = new Set();
let state = newGame(canvas.width, canvas.height);
let touch = null;

addEventListener('keydown', event => keys.add(event.key.toLowerCase()));
addEventListener('keyup', event => keys.delete(event.key.toLowerCase()));
canvas.addEventListener('pointerdown', event => (touch = { x: event.offsetX, y: event.offsetY }));
canvas.addEventListener('pointermove', event => touch && (touch = { x: event.offsetX, y: event.offsetY }));
addEventListener('pointerup', () => (touch = null));

function input() {
  const dir = { x: 0, y: 0 };
  if (keys.has('arrowleft') || keys.has('a')) dir.x -= 1;
  if (keys.has('arrowright') || keys.has('d')) dir.x += 1;
  if (keys.has('arrowup') || keys.has('w')) dir.y -= 1;
  if (keys.has('arrowdown') || keys.has('s')) dir.y += 1;
  if (touch) {
    const scale = canvas.width / canvas.clientWidth;
    dir.x = Math.sign(touch.x * scale - state.player.x);
    dir.y = Math.sign(touch.y * scale - state.player.y);
  }
  return dir;
}

let last = performance.now();
function frame(now) {
  state = step(state, input(), Math.min(0.05, (now - last) / 1000));
  last = now;
  ctx.fillStyle = '#1d2b3a';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#ffd23f';
  for (const star of state.stars) ctx.fillRect(star.x - 6, star.y - 6, 12, 12);
  ctx.fillStyle = '#5bd1ff';
  ctx.fillRect(state.player.x - 14, state.player.y - 14, 28, 28);
  document.getElementById('score').textContent = `Score: ${state.score}`;
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
