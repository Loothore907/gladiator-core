// The build page: describe a flow, confirm the closest test, settle its open choices, create a test of your own.
// The server (engine/serve.py) does all the deciding; this file only asks it and shows what it says. Everything
// from the server is put on the page as text, never as markup.
'use strict';
(() => {
  const STEPS = ['describe', 'match', 'choices', 'create'];
  const KEY = 'gladiator.build.session';
  const $ = (id) => document.getElementById(id);
  const state = {
    token: null,
    status: {},
    tests: [],
    found: null, // what /fork/describe answered
    test: null, // the test being forked: its questions and where each choice is used
    values: {}, // what is in each box, as typed
    look: null, // what /fork/review last answered
    asked: 0,
    wait: null,
    poll: null,
  };

  // ---- small helpers
  function el(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === false || value == null) continue;
      if (name === 'class') node.className = value;
      else if (name === 'text') node.textContent = value;
      else node.setAttribute(name, value === true ? '' : value);
    }
    for (const kid of kids) if (kid != null) node.append(kid);
    return node;
  }
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }
  function say(node, text) {
    node.textContent = text || '';
    node.hidden = !text;
  }
  function slug(text) {
    return String(text).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  }
  function list(lines) {
    return el('ul', { class: 'plainlist' }, ...lines.map((line) => el('li', { text: line })));
  }
  function button(label, kind, act) {
    const b = el('button', { type: 'button', class: kind, text: label });
    b.addEventListener('click', act);
    return b;
  }

  async function answer(res) {
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) throw new Error((data && data.error) || `The server answered ${res.status}. Try again.`);
    return data;
  }
  async function call(path, body) {
    let res;
    try {
      res = body === undefined
        ? await fetch(path)
        : await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...body, token: state.token || undefined }),
          });
    } catch (e) {
      throw new Error('Could not reach the server. Check your connection and try again.');
    }
    return answer(res);
  }

  function remember(token) {
    state.token = token || null;
    try {
      if (token) localStorage.setItem(KEY, token);
      else localStorage.removeItem(KEY);
    } catch (e) {
      /* a private window: the sign-in lasts until the page is closed */
    }
  }

  // ---- the steps: one open at a time, earlier ones folded to a line with "Change"
  function open(step, focus = true) {
    const at = STEPS.indexOf(step);
    STEPS.forEach((name, i) => {
      const card = $(name);
      card.hidden = i > at;
      card.classList.toggle('closed', i < at);
      const done = card.querySelector('.done');
      if (done) done.hidden = i >= at;
      const pill = document.querySelector(`#steps [data-step="${name}"]`);
      pill.classList.toggle('current', i === at);
      pill.classList.toggle('finished', i < at);
      if (i === at) pill.setAttribute('aria-current', 'step');
      else pill.removeAttribute('aria-current');
    });
    if (state.poll && step !== 'create') stopPolling();
    if (focus) {
      const title = $(step).querySelector('h2');
      title.setAttribute('tabindex', '-1');
      title.focus({ preventScroll: true });
      $(step).scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
  function fold(step, summary) {
    const done = clear($(step).querySelector('.done'));
    done.append(el('span', null, ...summary), button('Change', 'link', () => open(step)));
  }

  // ---- signing in
  async function refreshStatus() {
    try {
      state.status = await call('/fork/status', {});
    } catch (e) {
      state.status = {};
    }
    const s = state.status;
    if (state.token && s.needs_invite && !s.signed_in) remember(null); // the sign-in has expired
    let line = '';
    if (s.needs_invite === false) line = 'Running on your own machine';
    else if (s.signed_in) line = 'Signed in';
    else if (s.needs_invite) line = 'Not signed in.';
    $('who').textContent = line;
    $('signin-open').hidden = !(s.needs_invite && !s.signed_in);
    let hint = 'Your description is matched to the tests in the library by its words.';
    if (s.reads) {
      hint = `A model (${s.reader}) reads your description to find the test and to fill in the choices it ` +
        'already states. It only reads: the engine checks every value and you confirm each one. Leave out ' +
        'anything private.';
    } else if (s.reads_when_signed_in) {
      hint += ' For signed-in visitors a model reads it too, and fills in the choices it already states.';
    }
    $('describe-hint').textContent = hint;
  }
  async function signIn(code) {
    const got = await call('/signin', { invite: code });
    remember(got.token);
    await refreshStatus();
    if ($('create').hidden === false) showCreate();
  }
  function wireSignIn() {
    $('signin-open').addEventListener('click', () => {
      $('signin').hidden = false;
      $('signin-code').focus();
    });
    $('signin-close').addEventListener('click', () => {
      $('signin').hidden = true;
    });
    $('signin-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      say($('signin-error'), '');
      try {
        await signIn($('signin-code').value.trim());
        $('signin-code').value = '';
        $('signin').hidden = true;
      } catch (e) {
        say($('signin-error'), 'That code did not work. Check it and try again.');
      }
    });
  }

  // ---- step 1: describe
  async function describe(event) {
    event.preventDefault();
    const text = $('description').value.trim();
    const go = $('describe-form').querySelector('button');
    say($('describe-error'), '');
    if (!text) return say($('describe-error'), 'Describe the flow you want to test, in a sentence or two.');
    go.disabled = true;
    go.textContent = state.status.reads ? 'Reading your description...' : 'Looking...';
    try {
      state.found = await call('/fork/describe', { description: text });
      fold('describe', [el('strong', { text: 'Your flow: ' }), text.length > 160 ? text.slice(0, 157) + '...' : text]);
      showMatch();
      open('match');
    } catch (e) {
      say($('describe-error'), e.message);
    } finally {
      go.disabled = false;
      go.textContent = 'Find the closest test';
    }
  }

  // ---- step 2: the closest test
  function testCard(test, why, action) {
    return el(
      'div',
      { class: 'test' },
      el('h3', { text: test.name }),
      el('p', { text: test.about }),
      why ? el('p', { class: 'hint', text: why }) : null,
      el('p', { class: 'ref', text: test.ref }),
      action
    );
  }
  function showMatch() {
    const body = clear($('match-body'));
    const found = state.found;
    if (found.note) body.append(el('p', { class: 'hint', text: found.note }));
    const others = state.tests.filter((t) => !found.test || t.ref !== found.test.ref);
    if (found.test) {
      const match = found.matches.find((m) => m.ref === found.test.ref) || {};
      let why = '';
      if (found.read_by) why = `Why this one (${found.read_by} read your description): ${found.why}`;
      else if ((match.matched || []).length) why = `Why this one: your description shares these words with it: ${match.matched.join(', ')}.`;
      const closer = found.closer || [];
      if (closer.length) {
        body.append(
          el('p', { class: 'error', role: 'note' },
            el('strong', { text: closer.length === 1 ? 'A closer test exists, but it cannot be adapted yet: ' : 'Closer tests exist, but they cannot be adapted yet: ' }),
            closer.map((c) => `${c.name} (shares: ${c.matched.join(', ')})`).join('; ') + '. ' +
            'The test below is the closest one that has open choices. Start from it only if it is the same kind of job as yours.')
        );
      }
      body.append(
        el('p', {
          text: (closer.length ? 'The closest test you can adapt is this one. ' : 'This is the closest test in the library. ') +
            'Your own test starts as a copy of it, and the next step asks about each thing it lets you change.',
        }),
        testCard(found.test, why, button('Yes, start from this test', 'primary', () => use(found.test)))
      );
    } else {
      const lead = found.read_by
        ? `${found.read_by} read your description and found no test in the library that is the same kind of job.`
        : 'No test in the library matched the words in your description.';
      body.append(el('p', null, el('strong', { text: lead })));
      if (found.read_by && found.why) body.append(el('p', { class: 'hint', text: found.why }));
      if ((found.closer || []).length) {
        body.append(el('p', { text: 'Closest by its words, but with no open choices yet, so it cannot be adapted here: ' + found.closer.map((c) => c.name).join(', ') + '.' }));
      }
      body.append(
        el('p', {
          text: 'You can describe the flow in other words (use "Change" above), or start from a test below if it is close enough. A flow that is a different kind of job needs a new test in the library, which its authors add.',
        })
      );
    }
    if (others.length) {
      body.append(el('h3', { text: found.test ? 'Not the right one? The other tests you can start from' : 'Tests you can start from' }));
      for (const t of others) body.append(testCard(t, '', button('Start from this one', 'plain', () => choose(t.ref))));
    } else if (found.test) {
      body.append(el('p', { class: 'hint', text: 'This is the only test in the library with open choices so far. More are being added.' }));
    }
    body.append(el('p', { class: 'error', id: 'match-error', role: 'alert', hidden: true }));
  }
  async function choose(ref) {
    try {
      use(await call('/fork/choose', { ref }));
    } catch (e) {
      say($('match-error'), e.message);
    }
  }
  function use(test) {
    state.test = test;
    state.values = {};
    for (const q of test.questions) state.values[q.name] = String(q.value);
    fold('match', [el('strong', { text: 'Starting from: ' }), `${test.name} (${test.ref})`]);
    showChoices();
    open('choices');
    review();
  }

  // ---- step 3: the choices, and the rules they produce
  function allowed(q) {
    const unit = q.unit ? ` ${q.unit}` : '';
    if (q.type === 'whole_number') {
      const range = q.min != null && q.max != null ? `A whole number from ${q.min} to ${q.max}${unit}. ` : 'A whole number. ';
      return `${range}The library test uses ${q.default}.`;
    }
    return `One plain word, 2 to 20 lowercase letters. The library test uses "${q.default}".`;
  }
  function showChoices() {
    const test = state.test;
    const filled = test.questions.filter((q) => q.source).length;
    $('choices-lead').textContent =
      `${test.name} lets you change ${test.questions.length} things. ` +
      (filled
        ? `Your description settled ${filled} of them (marked below), so check those. Each of the others starts at the value the library test uses. `
        : 'Each starts at the value the library test uses. ') +
      'Change what is different in your flow and leave the rest. ' +
      'The panel headed "What a model will be told" shows the rules with your values, and updates as you type.';
    const form = clear($('choices-form'));
    for (const q of test.questions) {
      const id = `choice-${q.name}`;
      const number = q.type === 'whole_number';
      const input = el('input', {
        id,
        type: 'text',
        inputmode: number ? 'numeric' : null,
        autocomplete: 'off',
        maxlength: 24,
        value: state.values[q.name],
        'aria-describedby': `${id}-hint ${id}-error`,
      });
      const places = test.places[q.name];
      form.append(
        el(
          'div',
          { class: 'choice', 'data-choice': q.name },
          el('label', { for: id }, q.ask, q.source ? el('span', { class: 'tag', text: `from ${q.source}` }) : null),
          el('div', { class: 'field' }, input, q.unit ? el('span', { class: 'unit', text: q.unit }) : null),
          el('p', { class: 'hint', id: `${id}-hint`, text: allowed(q) + (places ? ` Used in ${places} places in this test.` : '') }),
          el('p', { class: 'error', id: `${id}-error`, role: 'alert', hidden: true })
        )
      );
      input.addEventListener('input', () => {
        state.values[q.name] = input.value;
        clearTimeout(state.wait);
        state.wait = setTimeout(review, 250);
      });
      input.addEventListener('focus', () => light(q.name, true));
      input.addEventListener('blur', () => {
        light(q.name, false);
        if (!input.value.trim()) {
          input.value = String(q.default); // an empty box means the library test's value: show it
          state.values[q.name] = input.value;
          review();
        }
      });
    }
    form.append(el('div', { id: 'together', hidden: true }));
    const note = clear($('not-covered'));
    const lines = [...(test.not_covered || [])];
    note.hidden = !(lines.length || (test.unusable || []).length);
    if (lines.length) {
      note.append(
        el('h3', { text: 'What this test cannot express' }),
        el('p', { text: 'Your description also asks for these. This test has no choice for them, so your test will not cover them:' }),
        list(lines)
      );
    }
    if ((test.unusable || []).length) {
      // "refund_cap: 9 is outside ..." reads better with the question in place of the choice's name
      const plain = test.unusable.map((line) => {
        const cut = line.indexOf(': ');
        const q = test.questions.find((x) => x.name === line.slice(0, cut));
        return q ? `${q.ask} ${line.slice(cut + 2)}` : line;
      });
      note.append(el('p', { text: 'These values from your description are not allowed, so the library test\'s values are shown instead:' }), list(plain));
    }
  }
  function light(name, on) {
    for (const piece of document.querySelectorAll('.piece')) {
      piece.classList.toggle('lit', on && piece.dataset.choice === name);
    }
  }
  async function review() {
    clearTimeout(state.wait);
    const asked = ++state.asked;
    const values = {};
    for (const [name, raw] of Object.entries(state.values)) if (raw.trim()) values[name] = raw.trim();
    try {
      const look = await call('/fork/review', { ref: state.test.ref, values });
      if (asked !== state.asked) return; // a newer answer is on its way
      state.look = look;
      say($('rules-error'), '');
      paintReview();
    } catch (e) {
      if (asked === state.asked) say($('rules-error'), e.message);
    }
  }
  function paintText(target, pieces, changed) {
    clear(target);
    for (const piece of pieces) {
      if (piece.choice) {
        const q = state.test.questions.find((x) => x.name === piece.choice);
        target.append(
          el('span', {
            class: 'piece' + (changed.has(piece.choice) ? ' changed' : ''),
            'data-choice': piece.choice,
            title: q ? q.ask : null,
            text: piece.text,
          })
        );
        continue;
      }
      piece.text.split(/ (?=\(\d+\) )/).forEach((part, i) => {
        if (i) target.append(el('span', { class: 'break' }), ' ');
        target.append(part);
      });
    }
  }
  function paintReview() {
    const look = state.look;
    const changed = new Set(look.changed.map((c) => c.name));
    const tangled = new Set();
    for (const rule of look.broken) rule.choices.forEach((name) => tangled.add(name));
    for (const q of state.test.questions) {
      const id = `choice-${q.name}`;
      const problem = look.problems[q.name];
      say($(`${id}-error`), problem ? problem.charAt(0).toUpperCase() + problem.slice(1) : '');
      $(id).setAttribute('aria-invalid', problem || tangled.has(q.name) ? 'true' : 'false');
    }
    const together = clear($('together'));
    together.hidden = !look.broken.length;
    if (look.broken.length) {
      together.append(el('p', { class: 'error', role: 'alert', text: 'These choices do not fit together: ' + look.broken.map((b) => b.say).join(' ') }));
    }
    const panel = document.querySelector('.rules');
    panel.classList.toggle('stale', !look.shown);
    if (look.shown) {
      paintText($('brief-text'), look.shown.brief, changed);
      paintText($('rules-text'), look.shown.rules, changed);
      const focused = document.activeElement && document.activeElement.closest('.choice');
      if (focused) light(focused.dataset.choice, true);
    }
    const count = state.test.questions.length;
    $('rules-also').textContent = look.shown
      ? `${changed.size} of ${count} choices changed from the library test` +
        (changed.size ? ' (the filled-in values). ' : '. ') +
        'Your choices also set the cases the test generates and how each decision is graded, not only this wording.'
      : 'Fix the choices marked in red to see the rules with your values.';
    $('choices-next').disabled = Boolean(Object.keys(look.problems).length || look.broken.length);
  }
  async function choicesDone() {
    await review();
    const look = state.look;
    if (!look || Object.keys(look.problems).length || look.broken.length) return;
    const summary = look.changed.length
      ? look.changed.map((c) => `${c.name.replace(/_/g, ' ')}: ${c.to}`).join(', ')
      : 'the same values as the library test';
    fold('choices', [el('strong', { text: 'Your choices: ' }), summary]);
    showCreate();
    open('create');
  }

  // ---- step 4: name it, check it, create it
  function testId() {
    const owner = slug($('test-owner').value);
    const name = slug($('test-name').value);
    return owner && name ? `${owner}/${name}` : null;
  }
  function command() {
    const base = state.look ? state.look.command : '';
    return testId() ? base.replace('yourname/your-test', testId()) : base;
  }
  function showId() {
    const id = testId();
    $('test-id').textContent = id
      ? `It will be filed as ${id}, version 1.0.0.`
      : 'Both are needed: together they are how the test is filed, like acme/returns-desk.';
    const shown = $('own-command');
    if (shown) shown.textContent = command();
  }
  function showCreate() {
    stopPolling();
    showId();
    const body = clear($('create-body'));
    const s = state.status;
    const error = el('p', { class: 'error', id: 'create-error', role: 'alert', hidden: true });
    if (s.can_create) {
      body.append(
        el('p', {
          text: 'Before your test is created, the engine checks it: it generates the test\'s cases with your values, plays them with a perfect player, and plays them again with players that cut corners (approving everything, skipping the lookups, ignoring a change). Your test is created only if the perfect player gets through and the corner-cutters do not. This takes a minute or two.',
        })
      );
      if (s.needs_invite && s.creates_left != null) {
        body.append(el('p', { class: 'hint', text: `You can create ${s.creates_left} more ${s.creates_left === 1 ? 'test' : 'tests'} with this sign-in.` }));
      }
      body.append(error, button('Check it and create it', 'primary', create));
    } else {
      const open_ = button('Sign in', 'plain', () => $('signin-open').click());
      body.append(
        el('p', null, el('strong', { text: 'Creating a test on this page is open to invited visitors for now.' }), ' You have two ways to finish:'),
        el('h3', { text: 'Sign in' }),
        el('p', { text: 'If you were given an invitation link or an access code, sign in and this page will check and create your test.' }),
        open_,
        el('h3', { text: 'Or create it on your own machine' }),
        el('p', { text: 'The engine is open. This command creates the same test with the choices you made here, and runs the same checks:' }),
        el('pre', { class: 'command', id: 'own-command', text: command() }),
        el('p', { class: 'hint' }, 'Get the engine from ', el('a', { href: 'https://github.com/Loothore907/gladiator-core', text: 'github.com/Loothore907/gladiator-core' }), '.')
      );
    }
    body.append(el('div', { id: 'job' }));
  }
  function stopPolling() {
    clearTimeout(state.poll);
    state.poll = null;
  }
  async function create() {
    const error = $('create-error');
    say(error, '');
    if (!testId()) return say(error, 'Give the test a name, and your name or team. Each needs a letter or a digit.');
    const go = $('create-body').querySelector('button.primary');
    go.disabled = true;
    const values = {};
    for (const [name, raw] of Object.entries(state.values)) if (raw.trim()) values[name] = raw.trim();
    try {
      const started = await call('/fork/create', {
        ref: state.test.ref,
        values,
        name: $('test-name').value.trim(),
        owner: $('test-owner').value.trim(),
      });
      go.hidden = true;
      watch(started.job);
    } catch (e) {
      go.disabled = false;
      say(error, e.message);
    }
  }
  function watch(id) {
    const box = clear($('job'));
    const line = el('p', { text: 'Starting the checks...' });
    const bar = el('div');
    box.append(el('div', { class: 'result' }, el('h3', { text: 'Checking your test' }), line, el('div', { class: 'progress', role: 'presentation' }, bar)));
    const ask = async () => {
      let job;
      try {
        job = await call(`/fork/job?id=${encodeURIComponent(id)}`);
      } catch (e) {
        line.textContent = e.message;
        return;
      }
      if (job.state === 'checking') {
        if (job.of) {
          line.textContent = `Checking difficulty level ${job.number} of ${job.of} (${job.level}). ${Math.round(job.seconds)} seconds so far.`;
          bar.style.width = `${Math.round(((job.number - 0.5) / job.of) * 100)}%`;
        }
        state.poll = setTimeout(ask, 1000);
        return;
      }
      finished(id, job);
      refreshStatus();
    };
    ask();
  }
  function finished(id, job) {
    const box = clear($('job'));
    if (job.state === 'created') {
      const suite = job.suite;
      box.append(
        el(
          'div',
          { class: 'result good' },
          el('h3', { text: 'Your test is ready' }),
          el('p', null, el('strong', { text: job.ref }), `, a copy of ${job.forked_from} with your choices. It passed every check in ${Math.round(job.seconds)} seconds and is sealed (fingerprint ${job.fingerprint}).`),
          el('p', { text: 'What the checks found:' }),
          list(job.checked || []),
          el('a', { class: 'download', href: `/fork/download?id=${encodeURIComponent(id)}`, download: '', text: 'Download your test' }),
          el('p', { class: 'hint', text: `The download is kept here for ${job.kept_minutes} minutes. It holds the sealed test, the record of these checks and a suite file, with a short note on where they go.` }),
          el('h3', { text: 'Then race two models on it' }),
          el('p', { text: 'Unzip the download at the top of your copy of the Gladiator engine, and run a match with any two models you have keys for:' }),
          el('pre', { class: 'command', text: `uv run gladiator match --suite ${suite} --a <provider:model> --b <provider:model> --max-usd 1` })
        )
      );
      return;
    }
    const refused = job.state === 'refused';
    box.append(
      el(
        'div',
        { class: 'result bad' },
        el('h3', { text: 'Your test was not created' }),
        el('p', {
          text: refused
            ? 'The checks found a problem with these values, so nothing was sealed. A test that lets a corner-cutting player through would not tell two models apart.'
            : 'Something went wrong while checking, so nothing was created.',
        }),
        list(job.problems || []),
        button('Change my choices', 'plain', () => open('choices'))
      )
    );
    const go = $('create-body').querySelector('button.primary');
    if (go) {
      go.hidden = false;
      go.disabled = false;
    }
  }

  // ---- start
  async function start() {
    try {
      state.token = localStorage.getItem(KEY);
    } catch (e) {
      state.token = null;
    }
    wireSignIn();
    $('describe-form').addEventListener('submit', describe);
    $('choices-form').addEventListener('submit', (event) => event.preventDefault());
    $('name-form').addEventListener('submit', (event) => event.preventDefault());
    $('choices-next').addEventListener('click', choicesDone);
    $('test-name').addEventListener('input', showId);
    $('test-owner').addEventListener('input', showId);
    open('describe', false);

    // An invitation link signs this browser in once; the invite then leaves the address bar.
    const url = new URL(location.href);
    const invite = url.searchParams.get('invite');
    if (invite) {
      url.searchParams.delete('invite');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
      try {
        remember((await call('/signin', { invite })).token);
      } catch (e) {
        $('signin').hidden = false;
        say($('signin-error'), 'That invitation did not work. If you have an access code, type it here.');
      }
    }
    await refreshStatus();
    try {
      state.tests = (await call('/fork/tests')).tests;
    } catch (e) {
      state.tests = [];
    }
  }
  start();
})();
