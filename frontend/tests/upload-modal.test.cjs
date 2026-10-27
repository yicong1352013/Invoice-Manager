const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('an upload freezes its manifest until it settles, then allows attachment association and retry', async () => {
  // Exercise the component's event handlers without a browser or network requests.
  const slots = [];
  const effects = [];
  const listeners = new Map();
  let cursor = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
    },
    useRef(initial) {
      const index = cursor++;
      return slots[index] ??= { current: initial };
    },
    useEffect(callback, deps) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        effects.push(() => {
          previous?.cleanup?.();
          slots[index] = { deps, cleanup: callback() };
        });
      }
    },
    useMemo: callback => callback(),
    useCallback: callback => callback,
  };
  const jsx = (type, props) => ({ type, props });
  const calls = { create: [], invoices: [], attachments: [], post: [], close: 0 };
  let finishCreate;
  const createPending = new Promise(resolve => { finishCreate = resolve; });
  const api = {
    __esModule: true,
    default: { post: async (...args) => { calls.post.push(args); } },
    uploadInvoices: async (...args) => {
      calls.invoices.push(args);
      return [{ status: 'success', id: 42, file_name: 'A.pdf' }];
    },
    getInvoice: async () => ({ id: 42, file_name: 'A.pdf', status: '已确认' }),
    uploadAttachments: async (...args) => {
      calls.attachments.push(args);
      return [{ status: 'success' }];
    },
  };
  const antd = Object.fromEntries(['Alert', 'Button', 'Input', 'Modal', 'Popconfirm', 'Select', 'Space', 'Table', 'Tag'].map(name => [name, name]));
  antd.Upload = { Dragger: 'Dragger', LIST_IGNORE: false };
  antd.message = { error() {}, warning() {}, success() {}, info() {} };
  const modules = {
    react,
    'react/jsx-runtime': { jsx, jsxs: jsx, Fragment: 'Fragment' },
    'react-router-dom': { useNavigate: () => () => {} },
    antd,
    '@ant-design/icons': {},
    dayjs: { __esModule: true, default: () => ({ format: () => '2026-10-26' }) },
    '../services/api': api,
    '../services/workflowApi': {
      createWorkflowRun: body => { calls.create.push(body); return createPending; },
      listWorkflows: async () => [],
      workflowError: String,
    },
    '../types/invoice': { InvoiceStatus: { PROCESSING: '解析中' } },
    '../types/attachment': { ATTACHMENT_CATEGORY_LABELS: { other: '其他', contract: '合同' } },
    './UploadModal.module.css': { default: {} },
  };
  const source = readFileSync(join(__dirname, '../src/components/UploadModal.tsx'), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const context = {
    exports: {}, require: name => { assert.ok(name in modules, `unexpected module: ${name}`); return modules[name]; },
    // LAN deployments use HTTP, where randomUUID is unavailable.
    crypto: { getRandomValues: array => require('node:crypto').webcrypto.getRandomValues(array) }, console,
    document: { addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name) },
    setInterval: () => 1, clearInterval() {},
  };
  vm.runInNewContext(compiled, context);
  let tree;
  const render = (open = true) => {
    cursor = 0;
    tree = context.exports.default({ open, onClose: () => { calls.close++; }, onCompleted() {} });
    effects.splice(0).forEach(effect => effect());
  };
  const elements = node => {
    if (Array.isArray(node)) return node.flatMap(elements);
    if (!node?.props) return [];
    return [node, ...['children', 'footer', 'message'].flatMap(key => elements(node.props[key]))];
  };
  const find = predicate => {
    const result = elements(tree).find(predicate);
    assert.ok(result, 'expected control is present');
    return result.props;
  };
  const named = label => find(node => node.props['aria-label'] === label);
  const button = label => find(node => node.type === 'Button' && node.props.children === label);
  const dragger = attachment => find(node => node.type === 'Dragger' && node.props.accept.includes('.docx') === attachment);
  const file = name => ({ name, size: 128 });
  render();
  find(node => node.props.id === 'upload-workflow').onChange(7);
  render();
  dragger(false).beforeUpload(file('A.pdf'));
  dragger(true).beforeUpload(file('receipt.pdf'));
  render();
  named('receipt.pdf附件类型').onChange('contract');
  find(node => node.props.placeholder === '备注').onChange({ target: { value: 'original note' } });
  render();

  const staleInvoiceDrop = dragger(false);
  const staleAttachmentDrop = dragger(true);
  const staleRemove = named('移除A.pdf');
  const staleRemoveAttachment = named('移除receipt.pdf');
  const staleCategory = named('receipt.pdf附件类型');
  const staleTarget = named('receipt.pdf关联发票');
  const staleNote = find(node => node.props.placeholder === '备注');
  const start = button('开始上传');
  const upload = start.onClick();
  const duplicate = start.onClick();
  staleInvoiceDrop.beforeUpload(file('B.pdf'));
  staleAttachmentDrop.beforeUpload(file('unexpected.pdf'));
  staleRemove.onClick();
  staleRemoveAttachment.onClick();
  staleCategory.onChange('other');
  staleTarget.onChange(999);
  staleNote.onChange({ target: { value: 'changed while uploading' } });
  find(node => node.props.id === 'upload-workflow').onChange(undefined);
  named('工作流批次名称').onChange({ target: { value: 'changed name' } });
  button('关闭').onClick();
  find(node => node.type === 'Modal' && node.props.title === '发票上传').onCancel();
  listeners.get('paste')({ clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => { assert.fail('paste must be ignored while uploading'); } }] } });
  assert.equal(calls.create.length, 1, 'a double click creates one batch');
  assert.equal(calls.close, 0, 'closing cannot discard the pending upload');
  render();
  assert.equal(dragger(false).disabled, true);
  assert.equal(named('移除A.pdf').disabled, true);
  assert.equal(named('receipt.pdf附件类型').disabled, true);
  assert.equal(named('receipt.pdf关联发票').disabled, true);
  assert.equal(find(node => node.props.placeholder === '备注').disabled, true);

  finishCreate({ id: 17 });
  await Promise.all([upload, duplicate]);
  render();
  assert.equal(calls.invoices.length, 1);
  assert.deepEqual(Array.from(calls.invoices[0][0], item => item.name), ['A.pdf']);
  assert.equal(calls.create[0].upload_manifest.invoices[0].name, 'A.pdf');
  assert.equal(calls.create[0].upload_manifest.attachments[0].name, 'receipt.pdf');
  const invoiceKey = calls.create[0].upload_manifest.invoices[0].id;
  const attachmentKey = calls.create[0].upload_manifest.attachments[0].id;
  assert.ok(invoiceKey && invoiceKey.length <= 100);
  assert.ok(attachmentKey && attachmentKey.length <= 100);
  assert.notEqual(invoiceKey, attachmentKey, 'invoice and attachment identities must be distinct');
  assert.deepEqual(Array.from(calls.invoices[0][2]), [invoiceKey], 'the upload retains its manifest identity');
  assert.equal(elements(tree).some(node => node.props['aria-label'] === '移除B.pdf'), false);
  assert.equal(find(node => node.props.id === 'upload-workflow').value, 7);
  assert.equal(named('工作流批次名称').value, '');
  assert.equal(named('receipt.pdf附件类型').value, 'contract');
  assert.equal(find(node => node.props.placeholder === '备注').value, 'original note');
  assert.equal(named('receipt.pdf关联发票').value, undefined);
  assert.equal(named('receipt.pdf关联发票').disabled, false, 'the existing manifest still permits association');
  assert.equal(dragger(false).disabled, true, 'new files remain outside the immutable batch manifest');
  button('关闭').onClick();
  render(false);
  render(true);
  assert.equal(named('receipt.pdf附件类型').value, 'contract', 'reopening retains the unfinished batch');
  named('receipt.pdf关联发票').onChange(42);
  find(node => node.props.placeholder === '备注').onChange({ target: { value: 'retry note' } });
  render();
  await button('开始上传').onClick();
  render();
  assert.equal(calls.create.length, 1, 'retry reuses the batch');
  assert.equal(calls.attachments.length, 1);
  assert.equal(calls.attachments[0][0], 42);
  assert.equal(calls.attachments[0][2], 'contract');
  assert.equal(calls.attachments[0][3], 'retry note');
  assert.equal(calls.attachments[0][4], 17);
  assert.deepEqual(Array.from(calls.attachments[0][5]), [attachmentKey], 'retry retains the attachment manifest identity');
  assert.equal(button('提交批次').disabled, false, 'all manifest entries were uploaded');
  const submit = button('提交批次');
  await Promise.all([submit.onClick(), submit.onClick()]);
  assert.deepEqual(calls.post, [['/workflow-runs/17/start']]);
});
