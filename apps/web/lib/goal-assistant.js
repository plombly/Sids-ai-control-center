// The goal box with its assistant: "Questionnaire" sends the idea to the
// goal assistant (Haiku reads the project, may ask up to 3 questions, then
// writes a brief you can edit) — shown as "Questionnaire"; "Send as written"
// submits the text as is; "Conversation" (1.3) is an open chat about the project
// (apps/api/goal_assist.py, mode chat) that "Write the goal" turns into the
// same editable brief.
// Used on the dashboard home ('home', with a project picker) and on each
// project's Overview ('project:<id>'). State lives here, so the pages can
// redraw around it every few seconds without losing what you typed.
import { requestJSON, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick } from './registry.js';
import { setHTML } from './dom.js';

const boxes = new Map(); // box id -> state
const options = new Map(); // box id -> { label, placeholder, extra, projectId }
const POLL_MS = 2000;
const WORKING = /^(queued|thinking)$/;

export function boxState(box) {
  if (!boxes.has(box)) boxes.set(box, { draft: '', atomic: false, session: null, answers: [], feedback: '', briefText: '', briefAtomic: false, revising: false, message: '', chatDraft: '', chatting: false });
  return boxes.get(box);
}

const domId = box => `assist-${box.replace(/[^a-z0-9-]/gi, '-')}`;
const attr = box => escValue(box);
const opt = value => (value ? esc(value) : '');

function ideaOf(session) {
  return session?.turns?.find(turn => turn.idea || turn.message)?.idea || session?.turns?.find(turn => turn.message)?.message || '';
}

// A chat message as HTML: escaped, then a little markdown (code blocks,
// `code`, **bold**, bullet and numbered lists, paragraphs).
export function chatText(text) {
  const blocks = String(text || '').split(/```/);
  return blocks
    .map((block, index) => {
      if (index % 2) return `<pre class="chat-code">${esc(block.replace(/^[a-z0-9+-]*\n/i, ''))}</pre>`;
      return block
        .split(/\n{2,}/)
        .map(paragraph => paragraph.trim())
        .filter(Boolean)
        .map(paragraph => {
          const inline = line => esc(line).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
          // Runs of list lines become lists; other lines stay text.
          const item = /^\s*([-*]|\d+[.)])\s+/;
          const out = [];
          let text = [];
          let list = null;
          const flushText = () => text.length && (out.push(`<p>${text.join('<br>')}</p>`), (text = []));
          const flushList = () => list && (out.push(`<${list.tag}>${list.items.join('')}</${list.tag}>`), (list = null));
          for (const line of paragraph.split('\n')) {
            if (item.test(line)) {
              flushText();
              const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
              if (list && list.tag !== tag) flushList();
              list = list || { tag, items: [] };
              list.items.push(`<li>${inline(line.replace(item, ''))}</li>`);
            } else {
              flushList();
              text.push(inline(line));
            }
          }
          flushText();
          flushList();
          return out.join('');
        })
        .join('');
    })
    .join('');
}

export function chatMarkup(box, state, session) {
  const working = WORKING.test(session.status);
  const bubbles = (session.turns || [])
    .filter(turn => turn.message)
    .map(turn => `<div class="chat-bubble ${turn.from === 'you' ? 'from-you' : 'from-laika'}"><span class="chat-who">${turn.from === 'you' ? 'You' : 'LAIka'}</span>${chatText(turn.message)}</div>`)
    .join('');
  const thinking = working
    ? `<div class="chat-bubble from-laika thinking"><span class="spinner" aria-hidden="true"></span> ${session.want === 'brief' ? 'Writing the goal from your conversation…' : 'Reading the project and thinking…'}</div>`
    : '';
  const failed = session.status === 'failed'
    ? `<div class="chat-bubble from-laika failed"><b>That did not work.</b> ${opt(session.error)} <button type="button" class="detail-button" data-assist-retry-turn="${attr(box)}">Try again</button></div>`
    : '';
  const id = `${domId(box)}-chat`;
  return `<p class="assist-step">Conversation <span class="subtle">· nothing is built until you start a goal</span></p><div class="chat-log" aria-live="polite" data-keep-scroll="chat-${attr(box)}" data-start-at="bottom">${bubbles}${thinking}${failed}</div><label class="sr-only" for="${id}">Your message</label><textarea id="${id}" class="chat-input" rows="2" data-assist-field="chatDraft" data-box="${attr(box)}" placeholder="${working ? 'Wait for the reply…' : 'Reply, ask, or suggest… (Enter sends, Shift+Enter for a new line)'}"${working ? ' disabled' : ''}>${opt(state.chatDraft)}</textarea><div class="composer-row"><span class="composer-buttons"><button type="button" data-assist-cancel="${attr(box)}">Start over</button>${session.brief?.goal ? `<button type="button" data-assist-to-brief="${attr(box)}"${working ? ' disabled' : ''}>Back to the goal</button>` : ''}<button type="button" data-assist-write-goal="${attr(box)}"${working ? ' disabled' : ''}>Write the goal</button><button type="button" class="primary" data-assist-send="${attr(box)}"${working ? ' disabled' : ''}>Send</button></span></div>`;
}

function idleMarkup(box, state, o) {
  const id = `${domId(box)}-text`;
  return `<label class="composer-label" for="${id}">${esc(o.label)}</label><textarea id="${id}" rows="3" data-assist-field="draft" data-box="${attr(box)}" placeholder="${escValue(o.placeholder)}">${opt(state.draft)}</textarea><div class="composer-row">${typeof o.extra === 'function' ? o.extra() : o.extra || ''}<label class="composer-atomic"><input type="checkbox" data-assist-field="atomic" data-box="${attr(box)}"${state.atomic ? ' checked' : ''}> Small change (one step)</label><span class="composer-buttons"><button type="button" data-assist-plain="${attr(box)}">Send as written</button><button type="button" data-assist-chat="${attr(box)}" title="Talk ideas through with Claude, as long as you like; nothing is built until you say so">Conversation</button><button type="button" class="primary" data-assist-start="${attr(box)}" title="LAIka asks up to three quick questions, then writes the goal for you to check">Questionnaire</button></span></div><p class="field-hint composer-modes"><b>Send as written</b>: starts now. <b>Questionnaire</b>: up to 3 quick questions, then a goal to check. <b>Conversation</b>: discuss ideas and features with Claude first; nothing is built until you say so.</p>`;
}

function ideaQuote(session) {
  const idea = ideaOf(session);
  return idea ? `<blockquote class="assist-idea">${esc(idea)}</blockquote>` : '';
}

function questionsMarkup(box, state, session) {
  const items = (session.questions || [])
    .map((question, index) => {
      const chips = (question.options || [])
        .map(choice => `<button type="button" class="template-chip${state.answers[index] === choice ? ' chosen' : ''}" data-assist-option="${attr(box)}" data-index="${index}" data-value="${escValue(choice)}">${esc(choice)}</button>`)
        .join('');
      return `<div class="assist-question"><label for="${domId(box)}-a${index}">${esc(question.question)}</label>${chips ? `<div class="template-chips">${chips}</div>` : ''}<input id="${domId(box)}-a${index}" data-assist-field="answer" data-index="${index}" data-box="${attr(box)}" value="${escValue(state.answers[index] || '')}" placeholder="Your answer (or leave empty: LAIka picks a sensible default)" autocomplete="off"></div>`;
    })
    .join('');
  return `<p class="assist-step">Questionnaire: a few questions first</p>${ideaQuote(session)}${items}<div class="composer-row"><span class="composer-buttons"><button type="button" data-assist-cancel="${attr(box)}">Start over</button><button type="button" data-assist-skip="${attr(box)}">Skip questions</button><button type="button" class="primary" data-assist-answer="${attr(box)}">Continue</button></span></div>`;
}

function briefMarkup(box, state, session) {
  const brief = session.brief || {};
  const revise = state.revising
    ? `<div class="assist-revise"><input data-assist-field="feedback" data-box="${attr(box)}" value="${escValue(state.feedback)}" placeholder="What should change? e.g. also support the P key" autocomplete="off"><button type="button" data-assist-revise="${attr(box)}">Rewrite brief</button></div>`
    : '';
  return `<p class="assist-step">Your goal, ready to start</p><div class="assist-brief-head"><b>${opt(brief.title)}</b>${brief.summary ? `<span class="subtle">${esc(brief.summary)}</span>` : ''}</div><textarea class="assist-brief" rows="12" data-assist-field="brief" data-box="${attr(box)}" aria-label="Goal brief (you can edit it)">${opt(state.briefText)}</textarea><span class="field-hint">You can edit this before starting. Nothing runs until you press Start.</span>${revise}<div class="composer-row"><label class="composer-atomic"><input type="checkbox" data-assist-field="briefAtomic" data-box="${attr(box)}"${state.briefAtomic ? ' checked' : ''}> Small change (one step)</label><span class="composer-buttons"><button type="button" data-assist-cancel="${attr(box)}">Start over</button>${session.mode === 'chat' ? `<button type="button" data-assist-to-chat="${attr(box)}">Keep talking</button>` : ''}${state.revising ? '' : `<button type="button" data-assist-change="${attr(box)}">Change something</button>`}<button type="button" class="primary" data-assist-submit="${attr(box)}">Start this goal</button></span></div>`;
}

export function assistantInner(box, state = boxState(box), o = options.get(box) || {}) {
  const session = state.session;
  const status = session?.status || 'idle';
  let body;
  if (session?.mode === 'chat' && (status !== 'brief' || state.chatting) && status !== 'submitted' && status !== 'cancelled') {
    body = chatMarkup(box, state, session);
  } else if (WORKING.test(status)) {
    body = `<p class="assist-step"><span class="spinner" aria-hidden="true"></span> ${status === 'queued' ? 'Starting…' : 'Reading the project and thinking…'} <span class="subtle">usually under a minute</span></p>${ideaQuote(session)}<div class="composer-row"><span class="composer-buttons"><button type="button" data-assist-cancel="${attr(box)}">Cancel</button></span></div>`;
  } else if (status === 'questions') body = questionsMarkup(box, state, session);
  else if (status === 'brief') body = briefMarkup(box, state, session);
  else if (status === 'failed') {
    body = `<p class="assist-step">The assistant could not finish</p><div class="form-status">${opt(session.error)}</div>${ideaQuote(session)}<div class="composer-row"><span class="composer-buttons"><button type="button" data-assist-cancel="${attr(box)}">Start over</button><button type="button" data-assist-plain="${attr(box)}">Send my idea as written</button><button type="button" class="primary" data-assist-retry="${attr(box)}">Try again</button></span></div>`;
  } else body = idleMarkup(box, state, o);
  return `${body}<span class="form-status" role="status">${opt(state.message)}</span>`;
}

// What a page puts where its goal box goes.
export function assistantMarkup(box, o) {
  options.set(box, { ...(options.get(box) || {}), ...o });
  restore(box);
  return `<div class="goal-assistant ${box === 'home' ? 'home-composer' : 'project-composer'}" id="${domId(box)}" data-assist-box="${attr(box)}">${assistantInner(box)}</div>`;
}

// The current session, reset to idle (keeps nothing of the old one).
function reset(state, message = '') {
  Object.assign(state, { session: null, answers: [], feedback: '', briefText: '', briefAtomic: false, revising: false, message, chatDraft: '', chatting: false });
  return state;
}

// Brief text/answers follow the session when it changes to a new step.
export function applySession(state, session) {
  const before = state.session?.status;
  state.session = session;
  if (session?.status === 'brief' && before !== 'brief') {
    state.briefText = session.brief?.goal || '';
    state.briefAtomic = Boolean(session.brief?.atomic);
    state.revising = false;
    state.feedback = '';
    state.chatting = false; // a new brief: show it
  }
  if (session?.status === 'questions' && before !== 'questions') state.answers = (session.questions || []).map(() => '');
  return state;
}

// A goal template (lib/project-kinds.js) goes into the box with its first
// blank selected; an assistant conversation in progress is left alone.
export function useTemplate(box, template) {
  const state = boxState(box);
  if (state.session) {
    state.message = 'Finish this goal or press Start over to use an idea';
    paintBox(box);
    return;
  }
  state.draft = template;
  paintBox(box);
  const area = typeof document !== 'undefined' && document.getElementById(`${domId(box)}-text`);
  if (area) {
    area.focus();
    const blank = template.indexOf('___');
    if (blank >= 0) area.setSelectionRange(blank, blank + 3);
  }
}

export function projectOf(box) {
  if (box.startsWith('project:')) return box.slice('project:'.length);
  if (typeof document === 'undefined') return 'laika';
  return document.getElementById('home-goal-project')?.value || 'laika';
}

const storage = {
  get(box) {
    try {
      return sessionStorage.getItem(`laika-assist:${box}`) || '';
    } catch {
      return '';
    }
  },
  set(box, id) {
    try {
      if (id) sessionStorage.setItem(`laika-assist:${box}`, id);
      else sessionStorage.removeItem(`laika-assist:${box}`);
    } catch {}
  }
};

const restored = new Set();
const pollers = new Map();

function paintBox(box) {
  if (typeof document === 'undefined') return;
  const node = document.getElementById(domId(box));
  // force: the person's own step; the chat log keeps its place (or follows new messages).
  if (node) setHTML(node, assistantInner(box), { force: true });
}

// The reply is in: put the cursor back in the message box, so Enter continues
// the conversation (unless the person is typing somewhere else).
// A new reply starts where you can read it (not scrolled past its beginning).
function showNewReply(box) {
  const log = document.querySelector(`#${domId(box)} .chat-log`);
  const last = log?.querySelector('.chat-bubble.from-laika:last-child');
  if (log && last) log.scrollTop = Math.max(0, last.offsetTop - 8);
}

function focusChat(box) {
  const input = document.getElementById(`${domId(box)}-chat`);
  const active = document.activeElement;
  if (input && !input.disabled && !(active && active !== document.body && active.matches?.('input, textarea, select'))) input.focus({ preventScroll: true });
}

function poll(box) {
  if (pollers.has(box)) return;
  const tick = async () => {
    const state = boxState(box);
    const id = state.session?.id;
    if (!id || !WORKING.test(state.session.status)) {
      pollers.delete(box);
      return;
    }
    try {
      applySession(state, await requestJSON(`/api/assistant/${encodeURIComponent(id)}`));
      if (!WORKING.test(state.session.status)) {
        paintBox(box);
        showNewReply(box);
        focusChat(box);
      }
    } catch (error) {
      state.message = error.message;
    }
    pollers.set(box, setTimeout(tick, POLL_MS));
  };
  pollers.set(box, setTimeout(tick, POLL_MS));
}

function restore(box) {
  if (restored.has(box) || typeof document === 'undefined') return;
  restored.add(box);
  const id = storage.get(box);
  if (!id || boxState(box).session) return;
  requestJSON(`/api/assistant/${encodeURIComponent(id)}`)
    .then(session => {
      if (/^(submitted|cancelled)$/.test(session.status)) return storage.set(box, '');
      applySession(boxState(box), session);
      paintBox(box);
      poll(box);
    })
    .catch(() => storage.set(box, ''));
}

async function act(box, button, work) {
  const state = boxState(box);
  if (button) button.disabled = true;
  state.message = '';
  try {
    await work(state);
  } catch (error) {
    state.message = error.message;
  }
  paintBox(box);
  if (state.session && WORKING.test(state.session.status)) poll(box);
}

async function start(state, box, idea, mode = 'plan') {
  const session = await requestJSON(`/api/projects/${encodeURIComponent(projectOf(box))}/assistant`, { method: 'POST', body: JSON.stringify({ idea, mode }) });
  applySession(state, session);
  storage.set(box, session.id);
}

const submitted = (response, state) =>
  reset(state, response?.duplicate ? 'LAIka is already working on that.' : `Started. It shows up under "In progress" in a moment.`);

if (typeof document !== 'undefined') {
  document.addEventListener('input', event => {
    const field = event.target?.dataset?.assistField;
    if (!field) return;
    const state = boxState(event.target.dataset.box);
    if (field === 'answer') state.answers[Number(event.target.dataset.index)] = event.target.value;
    else if (field === 'atomic' || field === 'briefAtomic') state[field] = event.target.checked;
    else state[{ draft: 'draft', brief: 'briefText', feedback: 'feedback', chatDraft: 'chatDraft' }[field]] = event.target.value;
  });
  // Enter sends a chat message; Shift+Enter is a new line.
  document.addEventListener('keydown', event => {
    if (event.target?.dataset?.assistField !== 'chatDraft' || event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    document.querySelector(`[data-assist-send="${CSS.escape(event.target.dataset.box)}"]`)?.click();
  });
  const sendChat = (box, button) =>
    act(box, button, async state => {
      const message = state.chatDraft.trim();
      if (!message) throw new Error('Write something first');
      applySession(state, await requestJSON(`/api/assistant/${encodeURIComponent(state.session.id)}/reply`, { method: 'POST', body: JSON.stringify({ message }) }));
      state.chatDraft = '';
      state.chatting = true;
    });
  registerClick('assistChat', button =>
    act(button.dataset.assistChat, button, async state => {
      const idea = state.draft.trim();
      if (!idea) throw new Error('Write what you would like to talk about first, e.g. "What could make the app better?"');
      await start(state, button.dataset.assistChat, idea, 'chat');
    })
  );
  registerClick('assistSend', button => sendChat(button.dataset.assistSend, button));
  registerClick('assistWriteGoal', button =>
    act(button.dataset.assistWriteGoal, button, async state => {
      applySession(state, await requestJSON(`/api/assistant/${encodeURIComponent(state.session.id)}/write-goal`, { method: 'POST' }));
      state.chatting = false;
    })
  );
  registerClick('assistRetryTurn', button =>
    act(button.dataset.assistRetryTurn, button, async state => {
      applySession(state, await requestJSON(`/api/assistant/${encodeURIComponent(state.session.id)}/retry`, { method: 'POST' }));
    })
  );
  registerClick('assistToChat', button => {
    boxState(button.dataset.assistToChat).chatting = true;
    paintBox(button.dataset.assistToChat);
  });
  registerClick('assistToBrief', button => {
    boxState(button.dataset.assistToBrief).chatting = false;
    paintBox(button.dataset.assistToBrief);
  });
  document.addEventListener('change', event => {
    const field = event.target?.dataset?.assistField;
    if (field === 'atomic' || field === 'briefAtomic') boxState(event.target.dataset.box)[field] = event.target.checked;
  });
  registerClick('assistStart', button =>
    act(button.dataset.assistStart, button, async state => {
      const idea = state.draft.trim();
      if (!idea) throw new Error('Describe what you want first');
      await start(state, button.dataset.assistStart, idea);
    })
  );
  registerClick('assistRetry', button =>
    act(button.dataset.assistRetry, button, state => start(state, button.dataset.assistRetry, ideaOf(state.session)))
  );
  registerClick('assistPlain', button =>
    act(button.dataset.assistPlain, button, async state => {
      const box = button.dataset.assistPlain;
      const goal = (state.session ? ideaOf(state.session) : state.draft).trim();
      if (!goal) throw new Error('Describe what you want first');
      const response = await requestJSON(`/api/projects/${encodeURIComponent(state.session?.project_id || projectOf(box))}/goals`, {
        method: 'POST',
        body: JSON.stringify({ goal, atomic: state.atomic, request_id: newRequestId() })
      });
      state.draft = '';
      storage.set(box, '');
      submitted(response, state);
    })
  );
  registerClick('assistOption', button => {
    const box = button.dataset.assistOption;
    boxState(box).answers[Number(button.dataset.index)] = button.dataset.value;
    paintBox(box);
  });
  const answer = (box, button, answers) =>
    act(box, button, async state => {
      applySession(state, await requestJSON(`/api/assistant/${encodeURIComponent(state.session.id)}/reply`, { method: 'POST', body: JSON.stringify({ answers }) }));
    });
  registerClick('assistAnswer', button => answer(button.dataset.assistAnswer, button, boxState(button.dataset.assistAnswer).answers.map(value => value || '')));
  registerClick('assistSkip', button => answer(button.dataset.assistSkip, button, []));
  registerClick('assistChange', button => {
    boxState(button.dataset.assistChange).revising = true;
    paintBox(button.dataset.assistChange);
    document.querySelector(`#${domId(button.dataset.assistChange)} [data-assist-field="feedback"]`)?.focus();
  });
  registerClick('assistRevise', button =>
    act(button.dataset.assistRevise, button, async state => {
      if (!state.feedback.trim()) throw new Error('Say what should change');
      applySession(state, await requestJSON(`/api/assistant/${encodeURIComponent(state.session.id)}/reply`, { method: 'POST', body: JSON.stringify({ feedback: state.feedback }) }));
    })
  );
  registerClick('assistSubmit', button =>
    act(button.dataset.assistSubmit, button, async state => {
      const goal = state.briefText.trim();
      if (!goal) throw new Error('The brief is empty');
      const response = await requestJSON(`/api/assistant/${encodeURIComponent(state.session.id)}/submit`, {
        method: 'POST',
        body: JSON.stringify({ goal, atomic: state.briefAtomic, request_id: newRequestId() })
      });
      state.draft = '';
      storage.set(button.dataset.assistSubmit, '');
      submitted(response, state);
    })
  );
  registerClick('assistCancel', button =>
    act(button.dataset.assistCancel, button, async state => {
      const id = state.session?.id;
      const idea = ideaOf(state.session);
      storage.set(button.dataset.assistCancel, '');
      reset(state);
      if (idea && !state.draft) state.draft = idea; // start over from the same text
      if (id) await requestJSON(`/api/assistant/${encodeURIComponent(id)}/cancel`, { method: 'POST' }).catch(() => null);
    })
  );
}
