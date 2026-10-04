// Game rules without the browser, so they can be tested with node --test.
export const SPEED = 260;

export function newGame(width, height, random = Math.random) {
  return { width, height, score: 0, player: { x: width / 2, y: height / 2 },
    stars: Array.from({ length: 5 }, () => ({ x: 20 + random() * (width - 40), y: 20 + random() * (height - 40) })) };
}

export function step(state, dir, seconds, random = Math.random) {
  const length = Math.hypot(dir.x, dir.y) || 1;
  const x = Math.min(state.width - 14, Math.max(14, state.player.x + (dir.x / length) * SPEED * seconds));
  const y = Math.min(state.height - 14, Math.max(14, state.player.y + (dir.y / length) * SPEED * seconds));
  let score = state.score;
  const stars = state.stars.map(star => {
    if (Math.abs(star.x - x) < 20 && Math.abs(star.y - y) < 20) {
      score += 1;
      return { x: 20 + random() * (state.width - 40), y: 20 + random() * (state.height - 40) };
    }
    return star;
  });
  return { ...state, player: { x, y }, stars, score };
}
