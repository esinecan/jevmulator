/**
 * Drives the pinned official TypeSafe JavaScript SDK against a running Jevmulator daemon.
 *
 * Pin: @typesafe-ai/sdk@0.6.0, contract commit 66880ccded6cb642dc1809620c2b108c33730214.
 *
 * Reads JEVMULATOR_BASE_URL and JEVMULATOR_API_KEY from the environment. Prints one line
 * per case and a JSON summary on the last line, then exits nonzero if any case failed.
 * It reaches only the daemon named by JEVMULATOR_BASE_URL. No provider is called.
 */

import {
  TypeSafeClient,
  VERSION,
  choice,
  noul,
  score,
  AuthenticationError,
  UnprocessableEntityError,
} from '@typesafe-ai/sdk';

const baseURL = process.env.JEVMULATOR_BASE_URL;
const apiKey = process.env.JEVMULATOR_API_KEY;

if (!baseURL || !apiKey) {
  console.error('JEVMULATOR_BASE_URL and JEVMULATOR_API_KEY must both be set.');
  process.exit(2);
}

const results = [];

async function check(name, body) {
  try {
    await body();
    results.push({ name, ok: true });
    console.log(`pass  ${name}`);
  } catch (error) {
    results.push({ name, ok: false, error: String(error && error.stack ? error.stack : error) });
    console.log(`FAIL  ${name}: ${error}`);
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function assertClose(actual, expected, tolerance, message) {
  assert(Math.abs(actual - expected) <= tolerance, `${message}: ${actual} vs ${expected}`);
}

const client = new TypeSafeClient({ apiKey, baseURL });

const STATE = {
  subject: 'Duplicate charge',
  message: 'I was charged twice for order 4417. Please refund one of them.',
};

await check('the installed SDK is the pinned version', async () => {
  assert(VERSION === '0.6.0', `expected 0.6.0, got ${VERSION}`);
});

await check('a noul question round trips', async () => {
  const response = await client.systemOne({
    state: STATE,
    questions: { billing: noul('Is this message about billing?') },
  });
  const answer = response.answers.billing;
  assert(answer.type === 'noul', 'answer type must be noul');
  assert(typeof answer.noul === 'number', 'noul must be a number');
  assert(answer.noul >= 0 && answer.noul <= 1, 'noul must lie in [0, 1]');
  assert(answer.confidence === undefined, 'a noul answer carries no confidence field');
});

await check('a choice question round trips', async () => {
  const response = await client.systemOne({
    state: STATE,
    questions: {
      tone: choice('What is the tone of this message?', {
        angry: 'An upset or hostile message',
        calm: 'A neutral or polite message',
        excited: 'An enthusiastic or eager message',
      }),
    },
  });
  const answer = response.answers.tone;
  assert(answer.type === 'choice', 'answer type must be choice');
  assert(['angry', 'calm', 'excited'].includes(answer.choice), 'choice must be a supplied option');
  const keys = Object.keys(answer.probabilities).sort();
  assert(
    JSON.stringify(keys) === JSON.stringify(['angry', 'calm', 'excited']),
    `probability keys must cover the criteria, got ${keys}`,
  );
  const total = Object.values(answer.probabilities).reduce((a, b) => a + b, 0);
  assertClose(total, 1, 0.02, 'probabilities must sum to approximately one');
  const best = Math.max(...Object.values(answer.probabilities));
  assertClose(answer.probabilities[answer.choice], best, 1e-9, 'choice must be an argmax');
  assert(answer.confidence >= 0 && answer.confidence <= 1, 'confidence must lie in [0, 1]');
});

await check('a score question round trips', async () => {
  const levels = ['Can wait', 'Needs attention this week', 'Needs attention today'];
  const response = await client.systemOne({
    state: STATE,
    questions: { urgency: score('How urgent is this message?', levels) },
  });
  const answer = response.answers.urgency;
  assert(answer.type === 'score', 'answer type must be score');
  assert(answer.score >= 0 && answer.score <= levels.length - 1, 'score must lie inside the scale');
  const legendKeys = Object.keys(answer.legend).sort();
  assert(
    JSON.stringify(legendKeys) === JSON.stringify(['0', '1', '2']),
    `legend keys must be the zero-based levels, got ${legendKeys}`,
  );
  levels.forEach((text, index) => {
    assert(answer.legend[String(index)] === text, `legend level ${index} must repeat the original`);
  });
  const probabilityKeys = Object.keys(answer.probabilities).sort();
  assert(
    JSON.stringify(probabilityKeys) === JSON.stringify(legendKeys),
    'probability keys must match the legend keys',
  );
  const expected = Object.entries(answer.probabilities).reduce(
    (total, [level, probability]) => total + Number(level) * probability,
    0,
  );
  assertClose(answer.score, expected, 1e-6, 'score must equal the expected value of its distribution');
});

await check('a mixed batch round trips', async () => {
  const response = await client.systemOne({
    state: STATE,
    questions: {
      billing: noul('Is this message about billing?'),
      tone: choice('What is the tone?', { angry: 'upset', calm: null }),
      urgency: score('How urgent?', ['later', 'now']),
    },
  });
  const keys = Object.keys(response.answers).sort();
  assert(
    JSON.stringify(keys) === JSON.stringify(['billing', 'tone', 'urgency']),
    `every question must be answered, got ${keys}`,
  );
  assert(response.answers.billing.type === 'noul', 'billing must be a noul answer');
  assert(response.answers.tone.type === 'choice', 'tone must be a choice answer');
  assert(response.answers.urgency.type === 'score', 'urgency must be a score answer');
});

await check('structured instructions and criteria survive', async () => {
  const response = await client.systemOne({
    state: { ticket: { body: 'refund please', tags: ['billing', 'urgent'] } },
    questions: {
      route: choice({ task: 'route this ticket' }, {
        billing: { when: 'money is involved' },
        technical: ['errors', 'outages'],
      }),
      depth: score({ task: 'how deep is the investigation' }, [
        'shallow',
        { label: 'medium', note: 'some digging' },
        ['deep', 'long'],
      ]),
    },
  });
  assert(['billing', 'technical'].includes(response.answers.route.choice), 'route must pick an option');
  const legend = response.answers.depth.legend;
  assert(legend['1'].label === 'medium', 'an object level description must survive intact');
  assert(Array.isArray(legend['2']), 'an array level description must survive intact');
});

await check('omitted instructions are accepted', async () => {
  const response = await client.systemOne({
    state: 'a message',
    questions: { q: noul() },
  });
  assert(response.answers.q.type === 'noul', 'a noul with null instructions must be answered');
});

await check('the default model alias is accepted and the emulator identity comes back', async () => {
  const response = await client.systemOne({
    state: 'a message',
    questions: { q: noul('Is this text?') },
  });
  assert(
    response.model === 'jevmulator-0.1.0-glm-5.3-flash',
    `expected the emulator identity, got ${response.model}`,
  );
});

await check('usage counts parse as nonnegative integers', async () => {
  const response = await client.systemOne({
    state: 'a message',
    questions: { q: noul('Is this text?') },
  });
  const { input_tokens: input, output_tokens: output } = response.usage;
  assert(Number.isInteger(input) && input >= 0, `input_tokens must be a nonnegative integer, got ${input}`);
  assert(Number.isInteger(output) && output >= 0, `output_tokens must be a nonnegative integer, got ${output}`);
});

await check('model discovery lists the catalogue', async () => {
  const models = await client.models.list();
  const names = models.map((model) => model.name);
  assert(names.includes('jev-latest'), 'jev-latest must be listed');
  assert(names.includes('jevmulator-0.1.0-glm-5.3-flash'), 'the resolved identity must be listed');
  models.forEach((model) => {
    assert(
      model.description.includes('glm-5.3-flash'),
      `every description must name the upstream model: ${model.description}`,
    );
    assert(/^\d{4}-\d{2}-\d{2}$/.test(model.release_date), `release_date must be YYYY-MM-DD, got ${model.release_date}`);
  });
});

await check('an explicit versioned model name is accepted', async () => {
  const response = await client.systemOne({
    state: 'a message',
    model: 'jevmulator-0.1.0-glm-5.3-flash',
    questions: { q: noul('Is this text?') },
  });
  assert(response.answers.q.type === 'noul', 'the versioned name must be accepted');
});

await check('a question id is returned unchanged, however unusual', async () => {
  const weirdId = 'a id with spaces, punctuation. and UPPER/lower';
  const response = await client.systemOne({
    state: 'a message',
    questions: { [weirdId]: noul('Is this text?') },
  });
  assert(weirdId in response.answers, 'the answer must come back under the submitted id');
});

await check('a bad key raises an authentication error', async () => {
  const badClient = new TypeSafeClient({ apiKey: 'not-the-key', baseURL });
  let raised = null;
  try {
    await badClient.systemOne({ state: 'a message', questions: { q: noul('Is this text?') } });
  } catch (error) {
    raised = error;
  }
  assert(raised !== null, 'a wrong key must raise');
  assert(
    raised instanceof AuthenticationError || String(raised).includes('401'),
    `expected an authentication error, got ${raised}`,
  );
});

await check('an unknown model raises a validation error', async () => {
  let raised = null;
  try {
    await client.systemOne({
      state: 'a message',
      model: 'gpt-4o-mini',
      questions: { q: noul('Is this text?') },
    });
  } catch (error) {
    raised = error;
  }
  assert(raised !== null, 'an unknown model must raise');
  assert(
    raised instanceof UnprocessableEntityError || String(raised).includes('422'),
    `expected a 422 error, got ${raised}`,
  );
});

await check('an empty question map raises a validation error', async () => {
  let raised = null;
  try {
    await client.systemOne({ state: 'a message', questions: {} });
  } catch (error) {
    raised = error;
  }
  assert(raised !== null, 'an empty question map must raise');
});

const failed = results.filter((result) => !result.ok);
console.log(
  JSON.stringify({
    sdk: VERSION,
    baseURL,
    total: results.length,
    passed: results.length - failed.length,
    failed: failed.length,
    failures: failed.map((result) => ({ name: result.name, error: result.error })),
  }),
);
// Set the exit code rather than calling process.exit, so Node drains its sockets and
// exits cleanly. Calling process.exit here aborted the runtime on Windows with 0xC0000409.
process.exitCode = failed.length === 0 ? 0 : 1;
