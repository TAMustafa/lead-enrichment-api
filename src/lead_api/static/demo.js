const statusExplanations={
DUPLICATE:'Already exists in Salesforce or the partner source. No new Lead was created and no existing record was overwritten.',
REVIEW_REQUIRED:'Needs review. No Salesforce record was written. Check the reason below.',
REJECTED:'Did not meet the qualification rules. No Salesforce record was written.',
QUALIFIED:'Qualified. See the saved outcome and Salesforce link below.'
};

// Single-request UI. CSV and history reuse these rendering/request functions.
const stages = [
  ['validation', 'Validate input'], ['identity', 'Establish identity'],
  ['resolution', 'Find business'], ['deduplication', 'Check duplicates'],
  ['enrichment', 'Get business details'], ['location', 'Check registered premises'],
  ['qualification', 'Apply rules'], ['mapping', 'Prepare Salesforce fields'],
  ['persistence', 'Save to Salesforce']
];
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let lastRequest = null, lastOutcome = null, busy = false, batchRunning = false;
let nodes = {}, started = 0;

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function reset() {
  nodes = {};
  $('steps').replaceChildren();
  $('result').hidden = true;
  $('result').replaceChildren();
  for (const [id, label] of stages) {
    const row = el('div', undefined, 'step');
    row.dataset.state = 'pending';
    const block = el('div');
    block.append(el('strong', label), el('small', 'Waiting'));
    row.append(el('span', '○', 'dot'), block);
    $('steps').append(row);
    nodes[id] = row;
  }
}

function step(event) {
  $('activity').textContent = event.detail;
  const node = nodes[event.stage];
  if (!node) return;
  node.dataset.state = event.state;
  node.querySelector('.dot').textContent = event.state === 'done' ? '✓' : '…';
  node.querySelector('small').textContent = event.detail;
  if (event.data) {
    const details = el('details');
    details.append(el('summary', 'Evidence'), el('pre', JSON.stringify(event.data, null, 2)));
    node.querySelector('details')?.remove();
    node.children[1].append(details);
  }
}

function settle() {
  for (const node of Object.values(nodes)) {
    if (node.dataset.state === 'pending') {
      node.dataset.state = 'skipped';
      node.querySelector('small').textContent = 'Not needed';
    } else if (node.dataset.state === 'running') {
      node.dataset.state = 'error';
      node.querySelector('small').textContent = 'Incomplete';
    }
  }
}

function result(event) {
  lastOutcome = event;
  const data = event.result, box = $('result');
  settle();
  box.hidden = false;
  box.replaceChildren();
  const status = el('span', data.qualification_status.replaceAll('_', ' '), 'status');
  status.dataset.status = data.qualification_status;
  box.append(status, el('p', statusExplanations[data.qualification_status]));
  if (['REJECTED', 'REVIEW_REQUIRED'].includes(data.qualification_status)) {
    box.append(el('p', data.qualification_reason_codes.map(code =>
      code.toLowerCase().replaceAll('_', ' ')).join(' · ')));
  }
  if (data.processing_metadata.idempotent_replay) {
    box.append(el('p', 'Previous result restored. No new Salesforce write.'));
  }
  if (event.salesforce_url) {
    const link = el('a', 'Open in Salesforce ↗');
    link.href = event.salesforce_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    box.append(link);
  }
  const details = el('details');
  details.append(el('summary', 'API response'), el('pre', JSON.stringify(data, null, 2)));
  box.append(details);
  $('activity').textContent = ((performance.now() - started) / 1000).toFixed(1) + 's · complete';
}

function error(message) {
  lastOutcome = {type: 'error', message};
  settle();
  $('result').hidden = false;
  $('result').replaceChildren(el('h2', 'Request incomplete'), el('p', message));
  $('activity').textContent = 'Action needed';
}

async function send(data) {
  if (busy) return;
  busy = true;
  lastOutcome = null;
  lastRequest = data;
  reset();
  started = performance.now();
  $('activity').textContent = 'Processing…';
  $('submit').disabled = $('retry').disabled = true;
  try {
    const response = await fetch('/demo/api/qualify', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Demo-Token': token},
      body: JSON.stringify(data)
    });
    if (!response.ok) {
      const body = await response.json();
      throw Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
    }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '', finished = false;
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'step') step(event);
        else if (event.type === 'result') { result(event); finished = true; }
        else if (event.type === 'error') { error(event.message); finished = true; }
      }
    }
    if (!finished) throw Error('Connection interrupted. Retry safely to recover this request.');
  } catch (failure) {
    error(failure.message);
  } finally {
    busy = false;
    $('submit').disabled = $('retry').disabled = batchRunning;
    $('retry').hidden = lastOutcome?.type !== 'error';
  }
}

$('form').addEventListener('submit', event => {
  event.preventDefault();
  send({business_name: $('name').value, address: $('address').value, source: $('source').value,
    target: $('target').value,
    source_lead_id: 'demo-' + crypto.randomUUID()});
});
$('retry').addEventListener('click', () => send(lastRequest));
reset();
