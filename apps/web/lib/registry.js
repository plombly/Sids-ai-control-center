// Extension points so new dashboard features live in their own module and do
// not edit render() or the click handler (parallel UI jobs stop colliding).
//
//   registerPanel(state => { ...update your own DOM nodes... })
//   registerClick('myAction', (button, event) => { ... })  // data-my-action="…"
//
// Panels run at the end of every render(); click handlers run for any button
// carrying the matching data-* attribute (dataset key in camelCase).
export const panels = [];
export const clickHandlers = new Map();
export const routeHandlers = [];

// onRoute(route => ...) runs on every navigation; route is
// { view: 'dashboard' | 'projects', projectId: string | null } (see router.js).
export const onRoute = handler => {
  routeHandlers.push(handler);
};

export const registerPanel = renderPanel => {
  panels.push(renderPanel);
};

export const registerClick = (datasetKey, handler) => {
  if (clickHandlers.has(datasetKey)) throw new Error(`click handler already registered: ${datasetKey}`);
  clickHandlers.set(datasetKey, handler);
};

export function renderPanels(state) {
  for (const renderPanel of panels) {
    try {
      renderPanel(state);
    } catch (error) {
      console.error('panel render failed', error);
    }
  }
}

export function dispatchClick(button, event) {
  for (const [key, handler] of clickHandlers) {
    if (button.dataset[key] !== undefined) handler(button, event);
  }
}
