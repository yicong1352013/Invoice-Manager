import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Input, Modal, Popconfirm, Select, Table, Tag, message } from 'antd';
import {
  DeleteOutlined, DownloadOutlined, EyeOutlined, FilePdfOutlined,
  PaperClipOutlined, RedoOutlined, UploadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import {
  deleteInvoice, getInvoice, getInvoiceFileUrl, listInvoices,
  reprocessInvoice, uploadAttachments, uploadInvoices,
} from '../services/api';
import type { Invoice } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';
import type { AttachmentCategory } from '../types/attachment';
import { ATTACHMENT_CATEGORY_LABELS } from '../types/attachment';
import styles from './UploadModal.module.css';

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
  const [existingOptions, setExistingOptions] = useState<InvoiceOption[]>([]);
  const [newOptions, setNewOptions] = useState<InvoiceOption[]>([]);
  const [lastNewInvoiceId, setLastNewInvoiceId] = useState<number | undefined>(undefined);
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const invoiceInputRef = useRef<HTMLInputElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const batchIdsRef = useRef<number[]>([]);
  const prevHadProcessingRef = useRef(false);

  const invoiceOptions = useMemo(
    () => [...newOptions, ...existingOptions.filter(
      (e) => !newOptions.some((n) => n.value === e.value)
    )],
    [newOptions, existingOptions]
  );

  const loadExistingOptions = useCallback(async () => {
    try {
      const data = await listInvoices({ page: 1, page_size: 100 });
      setExistingOptions(data.items.map((inv) => ({
        value: inv.id,
        // file name is what users recognize; invoice_number may still be blank
        label: `#${inv.id} ${inv.file_name}`,
      })));
    } catch {
      // non-fatal: this-batch invoices remain selectable
    }
  }, []);

  // Reset state whenever the modal opens
  useEffect(() => {
    if (open) {
      setInvoiceFiles([]);
      setAttachmentRows([]);
      setBatchInvoices([]);
      setNewOptions([]);
      setLastNewInvoiceId(undefined);
      prevHadProcessingRef.current = false;
      loadExistingOptions();
    }
  }, [open, loadExistingOptions]);

  const addFiles = (files: File[], allowedExts: string[], kind: 'invoice' | 'attachment') => {
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
      if (kind === 'invoice') {
        invoices.push({ uid: `${file.name}-${file.size}-${Date.now()}-${Math.random()}`, file });
      } else {
        // default target: the most recently uploaded invoice of this batch
        attachments.push({
          uid: `${file.name}-${file.size}-${Date.now()}-${Math.random()}`,
          file,
          category: 'other',
          targetInvoiceId: lastNewInvoiceId,
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
  }, [open, lastNewInvoiceId]);

  const closePreview = () => {
    if (preview) {
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
    if (invoiceFiles.length === 0 && attachmentRows.length === 0) {
      message.warning('请先选择发票或附件文件');
      return;
    }
    setUploading(true);
    try {
      // 1) invoices first — their ids become default attachment targets
      let uploadedIds: number[] = [];
      if (invoiceFiles.length > 0) {
        const results = await uploadInvoices(invoiceFiles.map((p) => p.file));
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
          setBatchInvoices(details);
          setNewOptions((prev) => [
            ...details.map((d) => ({ value: d.id, label: `#${d.id} ${d.file_name}（本次上传）` })),
            ...prev,
          ]);
          const lastId = okIds[okIds.length - 1];
          setLastNewInvoiceId(lastId);
          // default unassigned attachments to the most recently uploaded invoice
          setAttachmentRows((prev) => prev.map((r) => (r.targetInvoiceId ? r : { ...r, targetInvoiceId: lastId })));
        }
        setInvoiceFiles([]);
      }

      // 2) attachments — one request per file so each keeps its own note.
      // Already-associated rows are never touched; the fallback only feeds
      // unassigned rows and must prefer this click's uploads over the stale
      // lastNewInvoiceId state so it matches the backfill shown in the UI.
      if (attachmentRows.length > 0) {
        const fallbackTarget = uploadedIds[uploadedIds.length - 1] ?? lastNewInvoiceId;
        const unassigned = attachmentRows.filter((r) => !r.targetInvoiceId && !fallbackTarget);
        if (unassigned.length > 0) {
          message.warning('请为每个附件选择关联的发票');
          return;
        }

        let attachOk = 0;
        for (const row of attachmentRows) {
          const target = row.targetInvoiceId ?? fallbackTarget;
          try {
            const results = await uploadAttachments(target, [row.file], row.category, row.note);
            attachOk += results.filter((r) => r.status === 'success').length;
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
        setAttachmentRows([]);
      }

      if (uploadedIds.length > 0) {
        message.success(`已上传 ${uploadedIds.length} 张发票，正在识别…`);
      }
      onCompleted();
      loadExistingOptions();
    } catch (error) {
      message.error('上传失败，请重试');
      console.error(error);
    } finally {
      setUploading(false);
    }
  };

  const handleRowDelete = async (inv: Invoice) => {
    try {
      await deleteInvoice(inv.id);
      setBatchInvoices((prev) => prev.filter((i) => i.id !== inv.id));
      setNewOptions((prev) => prev.filter((o) => o.value !== inv.id));
      onCompleted();
    } catch {
      message.error('删除失败');
    }
  };

  const handleRowReprocess = async (inv: Invoice) => {
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
      render: (_, record) =>
        record.file_type === 'pdf' ? (
          <FilePdfOutlined style={{ fontSize: 24, color: 'var(--muted-foreground)' }} />
        ) : (
          <img
            src={getInvoiceFileUrl(record.id)}
            alt={record.file_name}
            className={styles.thumb}
            loading="lazy"
          />
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
      width: 150,
      render: (_, record) => (
        <>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => {
              onClose();
              navigate(`/invoices/${record.id}`);
            }}
          >
            详情
          </Button>
          <Button
            type="link"
            size="small"
            icon={<RedoOutlined />}
            onClick={() => handleRowReprocess(record)}
          >
            重新解析
          </Button>
          <Popconfirm title="确定删除该发票？" onConfirm={() => handleRowDelete(record)}>
            <Button type="link" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
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
      <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={onRemove} />
    </div>
  );

  return (
    <Modal
      title="发票上传"
      open={open}
      onCancel={() => {
        closePreview();
        onClose();
      }}
      width={1040}
      destroyOnClose
      footer={
        <div className={styles.footerBar}>
          <span className={styles.footerHint}>
            支持批量上传；发票上传后自动识别，附件将关联到所选发票
          </span>
          <span>
            <Button onClick={onClose} style={{ marginRight: 8 }}>
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
          </span>
        </div>
      }
    >
      {/* Section 1: invoice upload */}
      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <span className={styles.sectionLabel}>发票</span>
          <Button
            size="small"
            icon={<UploadOutlined />}
            onClick={() => invoiceInputRef.current?.click()}
          >
            上传文件
          </Button>
          <span className={styles.hint}>支持 Ctrl+V 粘贴截图 · 支持 jpg / jpeg / png / pdf，单文件不超过 10MB</span>
        </div>
        <input
          ref={invoiceInputRef}
          type="file"
          multiple
          accept={INVOICE_EXTS.map((e) => `.${e}`).join(',')}
          className={styles.hiddenInput}
          onChange={(e) => {
            addFiles(Array.from(e.target.files ?? []), INVOICE_EXTS, 'invoice');
            e.target.value = '';
          }}
        />
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
          <Button
            size="small"
            icon={<UploadOutlined />}
            onClick={() => attachmentInputRef.current?.click()}
          >
            上传文件
          </Button>
          <span className={styles.hint}>
            支持 jpg / jpeg / png / pdf / doc / docx / xls / xlsx，可关联到本次或已有发票
          </span>
        </div>
        <input
          ref={attachmentInputRef}
          type="file"
          multiple
          accept={ATTACHMENT_EXTS.map((e) => `.${e}`).join(',')}
          className={styles.hiddenInput}
          onChange={(e) => {
            addFiles(Array.from(e.target.files ?? []), ATTACHMENT_EXTS, 'attachment');
            e.target.value = '';
          }}
        />
        {attachmentRows.map((row) =>
          renderFileRow(
            row.uid,
            row.file.name,
            row.file,
            <>
              <Select
                size="small"
                style={{ width: 96 }}
                value={row.category}
                onChange={(category) => setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, category } : r))
                )}
                options={(Object.keys(ATTACHMENT_CATEGORY_LABELS) as AttachmentCategory[]).map((c) => ({
                  value: c,
                  label: ATTACHMENT_CATEGORY_LABELS[c],
                }))}
              />
              <Select
                size="small"
                showSearch
                style={{ width: 230 }}
                placeholder="选择关联发票"
                value={row.targetInvoiceId}
                onChange={(targetInvoiceId) => setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, targetInvoiceId } : r))
                )}
                optionFilterProp="label"
                options={invoiceOptions}
              />
              <Input
                size="small"
                style={{ width: 130 }}
                placeholder="备注"
                value={row.note}
                maxLength={200}
                onChange={(e) => setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, note: e.target.value } : r))
                )}
              />
            </>,
            () => setAttachmentRows((prev) => prev.filter((r) => r.uid !== row.uid))
          )
        )}
      </div>

      {/* File preview (local object URL) */}
      <Modal
        title={preview?.name}
        open={preview !== null}
        onCancel={closePreview}
        width={880}
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
                src={preview.url}
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
    </Modal>
  );
}
