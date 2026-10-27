import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Button, Input, Modal, Popconfirm, Select, Space, Table, Tag, Upload, message } from 'antd';
import {
  DeleteOutlined, DownloadOutlined, EyeOutlined, FilePdfOutlined,
  InboxOutlined, PaperClipOutlined, RedoOutlined, UploadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import {
  deleteInvoice, getInvoice, getInvoiceFileUrl,
  reprocessInvoice, uploadAttachments, uploadInvoices,
} from '../services/api';
import type { Invoice } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';
import type { AttachmentCategory } from '../types/attachment';
import { ATTACHMENT_CATEGORY_LABELS } from '../types/attachment';
import styles from './UploadModal.module.css';
import api from '../services/api';
import { createWorkflowRun, listWorkflows, workflowError, type WorkflowDefinition } from '../services/workflowApi';

const INVOICE_EXTS = ['pdf', 'jpg', 'jpeg', 'png'];
const ATTACHMENT_EXTS = [...INVOICE_EXTS, 'doc', 'docx', 'xls', 'xlsx'];
const MAX_FILE_MB = 10;
const PROCESSING_STATUSES = ['已上传', '解析中'];
const PREVIEWABLE_EXTS = [...INVOICE_EXTS];

const statusColors: Record<string, string> = {
  '已上传': 'default',
  '解析中': 'processing',
  '待处理': 'blue',
  '待审核': 'warning',
  '已确认': 'success',
  '已报销': 'green',
  '未报销': 'orange',
};

interface PendingFile {
  uid: string;
  file: File;
}

interface PendingAttachment {
  uid: string;
  file: File;
  category: AttachmentCategory;
  note?: string;
  targetInvoiceId?: number;
}

/** antd Select option: must use {value, label} — {id, label} renders but never selects */
interface InvoiceOption {
  value: number;
  label: string;
}

interface PreviewState {
  url: string;
  name: string;
  ext: string;
}

function formatSize(bytes: number): string {
  return bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)}MB`
    : `${Math.max(1, Math.round(bytes / 1024))}KB`;
}

function fileExt(name: string): string {
  return name.split('.').pop()?.toLowerCase() ?? '';
}

interface UploadModalProps {
  open: boolean;
  onClose: () => void;
  /** parent refresh hook, called after uploads and when recognition settles */
  onCompleted: () => void;
}

export default function UploadModal({ open, onClose, onCompleted }: UploadModalProps) {
  const navigate = useNavigate();
  const [invoiceFiles, setInvoiceFiles] = useState<PendingFile[]>([]);
  const [attachmentRows, setAttachmentRows] = useState<PendingAttachment[]>([]);
  const [batchInvoices, setBatchInvoices] = useState<Invoice[]>([]);
  const [newOptions, setNewOptions] = useState<InvoiceOption[]>([]);
  const [lastNewInvoiceId, setLastNewInvoiceId] = useState<number | undefined>(undefined);
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>([]);
  const [workflowId, setWorkflowId] = useState<number>();
  const [workflowRunId, setWorkflowRunId] = useState<number>();
  const [runName, setRunName] = useState('');
  const uploadingRef = useRef(false);
  const batchIdsRef = useRef<number[]>([]);
  const prevHadProcessingRef = useRef(false);

  // Reset state whenever the modal opens
  useEffect(() => {
    if (open) {
      listWorkflows().then(rows => setWorkflows(rows.filter(row => row.enabled))).catch(e => message.error(workflowError(e)));
      if (workflowRunId) return;
      setInvoiceFiles([]);
      setAttachmentRows([]);
      setBatchInvoices([]);
      setNewOptions([]);
      setLastNewInvoiceId(undefined);
      prevHadProcessingRef.current = false;
    }
  }, [open, workflowRunId]);

  const addFiles = (files: File[], allowedExts: string[], kind: 'invoice' | 'attachment') => {
    if (uploadingRef.current) return;
    if (workflowRunId) { message.warning('批次清单已锁定，请完成当前批次；需要增加文件时请新建批次。'); return; }
    const invoices: PendingFile[] = [];
    const attachments: PendingAttachment[] = [];
    for (const file of files) {
      const ext = fileExt(file.name);
      if (!allowedExts.includes(ext)) {
        message.error(`不支持的文件类型：${file.name}`);
        continue;
      }
      if (file.size / 1024 / 1024 >= MAX_FILE_MB) {
        message.error(`文件大小不能超过 ${MAX_FILE_MB}MB：${file.name}`);
        continue;
      }
      // getRandomValues also works on HTTP LAN deployments.
      const uid = crypto.getRandomValues(new Uint32Array(4)).join('-');
      if (kind === 'invoice') {
        invoices.push({ uid, file });
      } else {
        // default target: the most recently uploaded invoice of this batch
        attachments.push({
          uid,
          file,
          category: 'other',
          targetInvoiceId: !workflowId && newOptions.some(option => option.value === lastNewInvoiceId) ? lastNewInvoiceId : undefined,
        });
      }
    }
    if (invoices.length > 0) {
      setInvoiceFiles((prev) => [...prev, ...invoices]);
    }
    if (attachments.length > 0) {
      setAttachmentRows((prev) => [...prev, ...attachments]);
    }
  };

  // Ctrl+V paste screenshot support (Kingdee-style), active while modal is open
  useEffect(() => {
    if (!open) return;
    const onPaste = (e: ClipboardEvent) => {
      if (uploadingRef.current || workflowRunId) return;
      const images: File[] = [];
      for (const item of Array.from(e.clipboardData?.items ?? [])) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const blob = item.getAsFile();
          if (blob) {
            images.push(new File([blob], `粘贴截图-${dayjs().format('YYYYMMDD-HHmmss')}.png`, {
              type: blob.type || 'image/png',
            }));
          }
        }
      }
      if (images.length > 0) {
        addFiles(images, INVOICE_EXTS, 'invoice');
        message.success(`已从剪贴板添加 ${images.length} 张截图`);
      }
    };
    document.addEventListener('paste', onPaste);
    return () => document.removeEventListener('paste', onPaste);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, lastNewInvoiceId, workflowId, workflowRunId]);

  const closePreview = () => {
    if (preview?.url.startsWith('blob:')) {
      URL.revokeObjectURL(preview.url);
    }
    setPreview(null);
  };

  const openPreview = (file: File) => {
    const ext = fileExt(file.name);
    if (!PREVIEWABLE_EXTS.includes(ext)) {
      message.info('该格式暂不支持在线预览，可下载后查看');
    }
    setPreview({ url: URL.createObjectURL(file), name: file.name, ext });
  };

  /** Preview an uploaded invoice's original file on top of this modal. */
  const openInvoicePreview = (inv: Invoice) => {
    setPreview({ url: getInvoiceFileUrl(inv.id), name: inv.file_name, ext: inv.file_type });
  };

  const refreshBatch = useCallback(async () => {
    const ids = batchIdsRef.current;
    if (ids.length === 0) return;
    try {
      const details = await Promise.all(ids.map((id) => getInvoice(id)));
      setBatchInvoices(details);
    } catch {
      // keep the last snapshot; the next poll retries
    }
  }, []);

  // Poll recognition progress until the whole batch settles
  const hasProcessing = batchInvoices.some((inv) => PROCESSING_STATUSES.includes(inv.status));
  useEffect(() => {
    batchIdsRef.current = batchInvoices.map((inv) => inv.id);
  }, [batchInvoices]);
  useEffect(() => {
    if (!open || !hasProcessing) {
      if (prevHadProcessingRef.current && !hasProcessing && batchInvoices.length > 0) {
        prevHadProcessingRef.current = false;
        onCompleted();
      }
      return;
    }
    prevHadProcessingRef.current = true;
    const timer = setInterval(refreshBatch, 3000);
    return () => clearInterval(timer);
  }, [open, hasProcessing, batchInvoices.length, refreshBatch, onCompleted]);

  const handleStartUpload = async () => {
    if (uploadingRef.current) return;
    if (invoiceFiles.length === 0 && attachmentRows.length === 0) {
      message.warning('请先选择发票或附件文件');
      return;
    }
    uploadingRef.current = true;
    setUploading(true);
    try {
      let activeRunId = workflowRunId;
      if (workflowId && !activeRunId) {
        if (!invoiceFiles.length) { message.warning('工作流批次至少需要一张新发票'); return; }
        const run = await createWorkflowRun({ workflow_id: workflowId, name: runName.trim() || `发票批次 ${dayjs().format('YYYY-MM-DD HH:mm')}`, upload_manifest: { invoices: invoiceFiles.map(p => ({ id: p.uid, name: p.file.name })), attachments: attachmentRows.map(p => ({ id: p.uid, name: p.file.name })) } });
        activeRunId = run.id; setWorkflowRunId(run.id);
      }
      // 1) invoices first — their ids become default attachment targets
      let uploadedIds: number[] = [];
      if (invoiceFiles.length > 0) {
        const results = await uploadInvoices(invoiceFiles.map((p) => p.file), activeRunId, activeRunId ? invoiceFiles.map(p => p.uid) : undefined);
        const okIds: number[] = [];
        for (const r of results) {
          if (r.status === 'success' && r.id > 0) {
            okIds.push(r.id);
          } else {
            message.error(`${r.file_name}：${r.message}`);
          }
        }
        uploadedIds = okIds;
        if (okIds.length > 0) {
          const details = await Promise.all(okIds.map((id) => getInvoice(id)));
          setBatchInvoices(prev => [...prev.filter(p => !okIds.includes(p.id)), ...details]);
          setNewOptions((prev) => [
            ...details.map((d) => ({ value: d.id, label: `#${d.id} ${d.file_name}（本次上传）` })),
            ...prev.filter(option => !okIds.includes(option.value)),
          ]);
          const lastId = okIds[okIds.length - 1];
          setLastNewInvoiceId(lastId);
          // default unassigned attachments to the most recently uploaded invoice
          if (!workflowId) setAttachmentRows((prev) => prev.map((r) => (r.targetInvoiceId ? r : { ...r, targetInvoiceId: lastId })));
        }
        setInvoiceFiles(prev => prev.filter((_, index) => results[index]?.status !== 'success'));
      }

      // 2) attachments — one request per file so each keeps its own note.
      // Already-associated rows are never touched; the fallback only feeds
      // unassigned rows and must prefer this click's uploads over the stale
      // lastNewInvoiceId state so it matches the backfill shown in the UI.
      if (attachmentRows.length > 0) {
        const currentBatchIds = new Set([...newOptions.map(option => option.value), ...uploadedIds]);
        const fallbackTarget = workflowId ? undefined : uploadedIds[uploadedIds.length - 1] ?? (lastNewInvoiceId && currentBatchIds.has(lastNewInvoiceId) ? lastNewInvoiceId : undefined);
        const unassigned = attachmentRows.filter((row) => !currentBatchIds.has(row.targetInvoiceId ?? fallbackTarget ?? -1));
        if (unassigned.length > 0) {
          message.warning('请先上传本次发票，再为每个附件选择关联发票');
          return;
        }

        let attachOk = 0;
        const successfulRows: string[] = [];
        for (const row of attachmentRows) {
          const target = row.targetInvoiceId ?? fallbackTarget;
          if (!target) continue;
          try {
            const results = await uploadAttachments(target, [row.file], row.category, row.note, activeRunId, activeRunId ? [row.uid] : undefined);
            attachOk += results.filter((r) => r.status === 'success').length;
            if (results.every(r => r.status === 'success') && results.length) successfulRows.push(row.uid);
            for (const r of results) {
              if (r.status !== 'success') {
                message.error(`${r.file_name}：${r.message}`);
              }
            }
          } catch (error) {
            message.error(`附件上传失败：${row.file.name}`);
            console.error(error);
          }
        }
        if (attachOk > 0) {
          message.success(`已上传 ${attachOk} 个附件`);
        }
        setAttachmentRows(prev => prev.filter(row => !successfulRows.includes(row.uid)));
      }

      if (uploadedIds.length > 0) {
        message.success(workflowId ? `已上传 ${uploadedIds.length} 张发票，请关联附件后提交批次` : `已上传 ${uploadedIds.length} 张发票，正在识别…`);
      }
      onCompleted();
    } catch (error) {
      message.error(workflowError(error));
      console.error(error);
    } finally {
      uploadingRef.current = false;
      setUploading(false);
    }
  };

  const handleRowDelete = async (inv: Invoice) => {
    if (uploadingRef.current) return;
    try {
      await deleteInvoice(inv.id);
      setBatchInvoices((prev) => prev.filter((i) => i.id !== inv.id));
      setNewOptions((prev) => prev.filter((o) => o.value !== inv.id));
      setAttachmentRows(prev => prev.map(row => row.targetInvoiceId === inv.id ? { ...row, targetInvoiceId: undefined } : row));
      setLastNewInvoiceId(previous => previous === inv.id ? undefined : previous);
      onCompleted();
    } catch {
      message.error('删除失败');
    }
  };

  const handleRowReprocess = async (inv: Invoice) => {
    if (uploadingRef.current) return;
    try {
      await reprocessInvoice(inv.id);
      message.success(`发票 #${inv.id} 已重新开始识别`);
      setBatchInvoices((prev) => prev.map((i) =>
        i.id === inv.id ? { ...i, status: InvoiceStatus.PROCESSING } : i
      ));
      onCompleted();
    } catch {
      message.error('重新解析失败');
    }
  };

  const batchTotal = useMemo(() => {
    return batchInvoices.reduce((sum, inv) => sum + Number(inv.amount ?? 0), 0);
  }, [batchInvoices]);

  const batchColumns: ColumnsType<Invoice> = [
    {
      title: '序号',
      width: 56,
      render: (_, __, index) => index + 1,
    },
    {
      title: '缩略图',
      width: 60,
      render: (_, record) => (
        <span
          className={styles.thumbLink}
          title="点击预览"
          onClick={() => openInvoicePreview(record)}
        >
          {record.file_type === 'pdf' ? (
            <FilePdfOutlined style={{ fontSize: 24, color: 'var(--muted-foreground)' }} />
          ) : (
            <img
              src={getInvoiceFileUrl(record.id)}
              alt={record.file_name}
              className={styles.thumb}
              loading="lazy"
            />
          )}
        </span>
      ),
    },
    {
      title: '发票号码',
      dataIndex: 'invoice_number',
      ellipsis: true,
      render: (val: string | null) => val || '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: string) => <Tag color={statusColors[status] || 'default'}>{status}</Tag>,
    },
    {
      title: '开票日期',
      dataIndex: 'issue_date',
      width: 110,
      render: (val: string | null) => val || '-',
    },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 110,
      align: 'right',
      render: (val: number | null) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 210,
      render: (_, record) => (
        <>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => openInvoicePreview(record)}
          >
            预览
          </Button>
          <Button
            type="link"
            size="small"
            disabled={uploading}
            onClick={() => {
              if (uploadingRef.current) return;
              closePreview();
              onClose();
              navigate(`/invoices/${record.id}`);
            }}
          >
            详情
          </Button>
          {!workflowId && <Button
            type="link"
            size="small"
            icon={<RedoOutlined />}
            disabled={uploading}
            onClick={() => handleRowReprocess(record)}
          >
            重新解析
          </Button>}
          {!workflowId && <Popconfirm title="确定删除该发票？" onConfirm={() => handleRowDelete(record)}>
            <Button aria-label={`删除本次发票${record.file_name}`} disabled={uploading} type="link" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>}
        </>
      ),
    },
  ];

  const fileNameLink = (name: string, file: File) => (
    <a
      className={styles.fileNameLink}
      onClick={() => openPreview(file)}
      title="点击预览"
    >
      {name}
    </a>
  );

  const renderFileRow = (
    uid: string,
    name: string,
    file: File,
    extra: React.ReactNode,
    onRemove: () => void
  ) => (
    <div key={uid} className={styles.fileRow}>
      <PaperClipOutlined style={{ color: 'var(--muted-foreground)', fontSize: 12 }} />
      {fileNameLink(name, file)}
      {extra}
      <span className={styles.fileSize}>{formatSize(file.size)}</span>
      <Button aria-label={`移除${name}`} disabled={uploading || !!workflowRunId} type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => { if (!uploadingRef.current && !workflowRunId) onRemove(); }} />
    </div>
  );

  return (
    <>
      <Modal
        title="发票上传"
        open={open}
        closable={!uploading}
        maskClosable={!uploading}
        keyboard={!uploading}
        onCancel={() => {
          if (uploadingRef.current) return;
          closePreview();
          onClose();
        }}
        width={1040}
        destroyOnClose
        footer={
        <div className={styles.footerBar}>
          <span className={styles.footerHint}>
            {workflowId ? '先上传文件，再明确附件归属，全部完成后提交批次' : '支持批量上传；发票上传后自动识别，附件将关联到所选发票'}
          </span>
          <span>
            <Button disabled={uploading} onClick={() => { if (!uploadingRef.current) onClose(); }} style={{ marginRight: 8 }}>
              关闭
            </Button>
            <Button
              type="primary"
              icon={<UploadOutlined />}
              loading={uploading}
              onClick={handleStartUpload}
              disabled={invoiceFiles.length === 0 && attachmentRows.length === 0}
            >
              开始上传
            </Button>
            {workflowRunId && <Button style={{ marginLeft: 8 }} type="primary" loading={uploading} disabled={invoiceFiles.length > 0 || attachmentRows.length > 0} onClick={async () => {
              if (uploadingRef.current) return;
              uploadingRef.current = true;
              setUploading(true);
              try { await api.post(`/workflow-runs/${workflowRunId}/start`); const completedId = workflowRunId; setWorkflowRunId(undefined); setWorkflowId(undefined); onCompleted(); onClose(); navigate(`/workflows/runs/${completedId}`); }
              catch (e) { message.error(workflowError(e)); } finally { uploadingRef.current = false; setUploading(false); }
            }}>提交批次</Button>}
          </span>
        </div>
      }
    >
      <div className={styles.section}><Space wrap><label htmlFor="upload-workflow">处理方式</label><Select id="upload-workflow" style={{ width: 260 }} allowClear disabled={!!workflowRunId || batchInvoices.length > 0 || uploading} value={workflowId} placeholder="普通上传（自动识别）" onChange={id => { if (uploadingRef.current) return; setWorkflowId(id); if (id) setAttachmentRows(prev => prev.map(row => ({ ...row, targetInvoiceId: undefined }))); }} options={workflows.map(w => ({ value: w.id, label: w.name }))} />{workflowId && <Input aria-label="工作流批次名称" style={{ width: 260 }} disabled={uploading || !!workflowRunId} value={runName} onChange={e => { if (!uploadingRef.current) setRunName(e.target.value); }} placeholder="批次名称（可选）" />}</Space>{workflowRunId && <Alert style={{ marginTop: 12 }} type="info" showIcon message={<span>已创建批次 #{workflowRunId}，清单已锁定。关闭后可在本页面重新打开继续。<Button type="link" disabled={uploading} onClick={() => { if (uploadingRef.current) return; onClose(); navigate(`/workflows/runs/${workflowRunId}`); }}>查看批次</Button><Popconfirm title="取消当前批次？已上传文件将保留。" disabled={uploading} onConfirm={async () => { if (uploadingRef.current) return; try { await api.post(`/workflow-runs/${workflowRunId}/cancel`); setWorkflowRunId(undefined); setInvoiceFiles([]); setAttachmentRows([]); setBatchInvoices([]); setNewOptions([]); } catch (e) { message.error(workflowError(e)); } }}><Button type="link" disabled={uploading} danger>取消批次</Button></Popconfirm></span>} />}</div>
      {/* Section 1: invoice upload */}
      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <span className={styles.sectionLabel}>发票</span>
        </div>
        <Upload.Dragger
          multiple
          disabled={uploading || !!workflowRunId}
          accept={INVOICE_EXTS.map((e) => `.${e}`).join(',')}
          showUploadList={false}
          beforeUpload={(file) => {
            addFiles([file], INVOICE_EXTS, 'invoice');
            return Upload.LIST_IGNORE;
          }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽发票文件到此处上传</p>
          <p className="ant-upload-hint">
            支持 jpg / jpeg / png / pdf 格式，单文件不超过 10MB，可批量选择；支持 Ctrl+V 直接粘贴截图
          </p>
        </Upload.Dragger>
        {invoiceFiles.map((p) =>
          renderFileRow(p.uid, p.file.name, p.file, null, () =>
            setInvoiceFiles((prev) => prev.filter((f) => f.uid !== p.uid))
          )
        )}
      </div>

      {/* Section 2: this batch's recognized invoices */}
      {batchInvoices.length > 0 && (
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <span className={styles.sectionLabel}>本次上传明细</span>
          </div>
          <Table<Invoice>
            className={styles.batchTable}
            rowKey="id"
            size="small"
            columns={batchColumns}
            dataSource={batchInvoices}
            pagination={false}
            summary={() => (
              <Table.Summary.Row>
                <Table.Summary.Cell index={0} className={styles.summaryCell} colSpan={5}>
                  合计（{batchInvoices.length} 张）
                </Table.Summary.Cell>
                <Table.Summary.Cell index={1} align="right" className={styles.summaryCell}>
                  ¥{batchTotal.toFixed(2)}
                </Table.Summary.Cell>
                <Table.Summary.Cell index={2} className={styles.summaryCell} />
              </Table.Summary.Row>
            )}
          />
        </div>
      )}

      {/* Section 3: attachments */}
      <div className={styles.section} style={{ marginBottom: 0 }}>
        <div className={styles.sectionHeader}>
          <span className={styles.sectionLabel}>附件</span>
        </div>
        <Upload.Dragger
          multiple
          disabled={uploading || !!workflowRunId}
          accept={ATTACHMENT_EXTS.map((e) => `.${e}`).join(',')}
          showUploadList={false}
          beforeUpload={(file) => {
            addFiles([file], ATTACHMENT_EXTS, 'attachment');
            return Upload.LIST_IGNORE;
          }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽附件文件到此处上传</p>
          <p className="ant-upload-hint">
            支持 jpg / jpeg / png / pdf / doc / docx / xls / xlsx 格式，先上传本次发票后关联附件并填写备注
          </p>
        </Upload.Dragger>
        {attachmentRows.map((row) =>
          renderFileRow(
            row.uid,
            row.file.name,
            row.file,
            <>
              <Select
                size="small"
                disabled={uploading}
                aria-label={`${row.file.name}附件类型`}
                style={{ width: 96 }}
                value={row.category}
                onChange={(category) => { if (uploadingRef.current) return; setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, category } : r))
                ); }}
                options={(Object.keys(ATTACHMENT_CATEGORY_LABELS) as AttachmentCategory[]).map((c) => ({
                  value: c,
                  label: ATTACHMENT_CATEGORY_LABELS[c],
                }))}
              />
              <Select
                size="small"
                disabled={uploading}
                showSearch
                aria-label={`${row.file.name}关联发票`}
                style={{ width: 230 }}
                placeholder={newOptions.length ? '选择本次上传的发票' : '先上传本次发票后关联'}
                value={row.targetInvoiceId}
                onChange={(targetInvoiceId) => { if (uploadingRef.current) return; setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, targetInvoiceId } : r))
                ); }}
                optionFilterProp="label"
                options={newOptions}
              />
              <Input
                size="small"
                disabled={uploading}
                style={{ width: 130 }}
                placeholder="备注"
                value={row.note}
                maxLength={200}
                onChange={(e) => { if (uploadingRef.current) return; setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, note: e.target.value } : r))
                ); }}
              />
            </>,
            () => setAttachmentRows((prev) => prev.filter((r) => r.uid !== row.uid))
          )
        )}
      </div>
      </Modal>

      {/* File preview (local object URL or an uploaded invoice original) —
          rendered as a sibling so it never participates in the upload
          modal's lifecycle and stays on top of it */}
      <Modal
        title={preview?.name}
        open={preview !== null}
        onCancel={closePreview}
        width={880}
        zIndex={1050}
        footer={
          preview && (
            <a href={preview.url} download={preview.name}>
              <Button icon={<DownloadOutlined />}>下载</Button>
            </a>
          )
        }
      >
        {preview && (
          <div className={styles.previewBox}>
            {preview.ext === 'pdf' ? (
              <iframe
                src={`${preview.url}#zoom=page-fit`}
                title={preview.name}
                className={styles.previewFrame}
              />
            ) : PREVIEWABLE_EXTS.includes(preview.ext) ? (
              <img src={preview.url} alt={preview.name} className={styles.previewFrame} />
            ) : (
              <div className={styles.previewUnsupported}>
                该格式（{preview.ext}）暂不支持在线预览，请下载后查看
              </div>
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
