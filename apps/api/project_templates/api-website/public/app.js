// The page talks to the server's API on the same address (no hard-coded host).
const list = document.getElementById('notes');

async function refresh() {
  const notes = await (await fetch('/api/notes')).json();
  list.replaceChildren(...notes.map(note => Object.assign(document.createElement('li'), { textContent: note.text })));
}

document.getElementById('add').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  await fetch('/api/notes', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: form.text.value }) });
  form.reset();
  refresh();
});
refresh();
