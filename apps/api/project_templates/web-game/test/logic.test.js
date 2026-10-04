import test from 'node:test';
import assert from 'node:assert/strict';
import { newGame, step } from '../public/logic.js';

test('the player moves, stays inside and scores on a star', () => {
  let state = newGame(800, 500, () => 0.5);
  state = { ...state, stars: [{ x: state.player.x + 10, y: state.player.y }] };
  state = step(state, { x: 1, y: 0 }, 0.01, () => 0);
  assert.equal(state.score, 1);
  for (let i = 0; i < 200; i += 1) state = step(state, { x: -1, y: 0 }, 0.05);
  assert.equal(state.player.x, 14);
});
