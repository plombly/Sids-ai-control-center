import test from 'node:test';
import assert from 'node:assert/strict';
import { applySession, assistantInner, assistantMarkup, boxState, projectOf } from './lib/goal-assistant.js';

const opts = { label: 'What should SID do in <G>?', placeholder: 'Describe it' };

test('idle box: typed text survives redraws; both ways to send', () => {
  const state = boxState('project:g');
  state.draft = 'add <a> pause menu';
  const html = assistantMarkup('project:g', opts);
  assert.match(html, /id="assist-project-g"/);
  assert.match(html, /What should SID do in &lt;G&gt;\?/);
  assert.match(html, />add &lt;a&gt; pause menu<\/textarea>/);
  assert.match(html, /data-assist-plain="project:g">Send as written/);
  assert.match(html, /data-assist-start="project:g">Plan it with me/);
  assert.doesNotMatch(html, /—/);
  assert.equal(projectOf('project:g'), 'g');
});

test('thinking, questions with suggested answers, then an editable brief', () => {
  const state = boxState('project:q');
  applySession(state, { id: 's1', status: 'thinking', turns: [{ from: 'you', idea: 'pause <menu>' }] });
  let html = assistantInner('project:q', state, opts);
  assert.match(html, /Reading the project/);
  assert.match(html, /pause &lt;menu&gt;/);
  assert.match(html, /data-assist-cancel/);
  applySession(state, { id: 's1', status: 'questions', turns: [], questions: [{ question: 'Which key?', options: ['Esc', 'P'] }, { question: 'Sound?', options: [] }] });
  assert.deepEqual(state.answers, ['', '']);
  state.answers[0] = 'Esc';
  html = assistantInner('project:q', state, opts);
  assert.match(html, /class="template-chip chosen"[^>]*data-value="Esc"/);
  assert.match(html, /data-assist-field="answer" data-index="1"/);
  assert.match(html, /data-assist-skip/);
  applySession(state, { id: 's1', status: 'brief', turns: [], brief: { title: 'Pause', summary: 'Adds <pause>.', goal: 'Add a pause menu.', atomic: true } });
  assert.equal(state.briefText, 'Add a pause menu.');
  assert.equal(state.briefAtomic, true);
  state.briefText = 'edited';
  applySession(state, { id: 's1', status: 'brief', brief: { title: 'Pause', summary: 'Adds <pause>.', goal: 'Add a pause menu.' } });
  assert.equal(state.briefText, 'edited'); // a poll of the same step keeps edits
  html = assistantInner('project:q', state, opts);
  assert.match(html, />edited<\/textarea>/);
  assert.match(html, /Adds &lt;pause&gt;\./);
  assert.match(html, /data-assist-submit="project:q">Start this goal/);
  assert.match(html, /data-assist-change/);
  state.revising = true;
  assert.match(assistantInner('project:q', state, opts), /data-assist-field="feedback"/);
});

test('a failed session offers retry and sending the idea as written', () => {
  const state = boxState('home');
  applySession(state, { id: 's2', status: 'failed', error: 'Claude is <paused>', turns: [{ from: 'you', idea: 'x' }] });
  const html = assistantInner('home', state, { ...opts, extra: () => '<select id="home-goal-project"></select>' });
  assert.match(html, /Claude is &lt;paused&gt;/);
  assert.match(html, /data-assist-retry="home"/);
  assert.match(html, /data-assist-plain="home">Send my idea as written/);
});
