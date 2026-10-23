import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Modal, Popconfirm, Select, Table, Tag, message } from 'antd';
import {
  DeleteOutlined, EyeOutlined, FilePdfOutlined, PaperClipOutlined,
  RedoOutlined, UploadOutlined,
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
  targetInvoiceId?: number;
}

interface InvoiceOption {
  id: number;
  label: string;
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
  const [uploading, setUploading] = useState(false);
  const invoiceInputRef = useRef<HTMLInputElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const batchIdsRef = useRef<number[]>([]);
  const prevHadProcessingRef = useRef(false);

  const invoiceOptions = useMemo(
    () => [...newOptions, ...existingOptions.filter(
      (e) => !newOptions.some((n) => n.id === e.id)
    )],
    [newOptions, existingOptions]
  );

  const loadExistingOptions = useCallback(async () => {
    try {
      const data = await listInvoices({ page: 1, page_size: 100 });
      setExistingOptions(data.items.map((inv) => ({
        id: inv.id,
        label: `#${inv.id} ${inv.invoice_number || inv.file_name}`,
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
      prevHadProcessingRef.current = false;
      loadExistingOptions();
    }
  }, [open, loadExistingOptions]);

  const addFiles = (files: File[], allowedExts: string[], kind: 'invoice' | 'attachment') => {
    const accepted: PendingFile[] = [];
    const acceptedAttachments: PendingAttachment[] = [];
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
        accepted.push({ uid: `${file.name}-${file.size}-${Date.now()}-${Math.random()}`, file });
      } else {
        acceptedAttachments.push({
          uid: `${file.name}-${file.size}-${Date.now()}-${Math.random()}`,
          file,
          category: 'other',
        });
      }
    }
    if (kind === 'invoice' && accepted.length > 0) {
      setInvoiceFiles((prev) => [...prev, ...accepted]);
    }
    if (kind === 'attachment' && acceptedAttachments.length > 0) {
      setAttachmentRows((prev) => [...prev, ...acceptedAttachments]);
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
  }, [open]);

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
            ...details.map((d) => ({ id: d.id, label: `#${d.id} ${d.invoice_number || d.file_name}（本次上传）` })),
            ...prev,
          ]);
        }
        setInvoiceFiles([]);
      }

      // 2) attachments — each row needs a target invoice
      if (attachmentRows.length > 0) {
        const fallbackTarget = uploadedIds[0];
        const unassigned = attachmentRows.filter((r) => !r.targetInvoiceId && !fallbackTarget);
        if (unassigned.length > 0) {
          message.warning('请为每个附件选择关联的发票');
          return;
        }

        const groups = new Map<string, { invoiceId: number; category: AttachmentCategory; files: File[] }>();
        for (const row of attachmentRows) {
          const target = row.targetInvoiceId ?? fallbackTarget;
          const key = `${target}:${row.category}`;
          if (!groups.has(key)) {
            groups.set(key, { invoiceId: target, category: row.category, files: [] });
          }
          groups.get(key)!.files.push(row.file);
        }

        let attachOk = 0;
        for (const group of groups.values()) {
          try {
            const results = await uploadAttachments(group.invoiceId, group.files, group.category);
            attachOk += results.filter((r) => r.status === 'success').length;
            for (const r of results) {
              if (r.status !== 'success') {
                message.error(`${r.file_name}：${r.message}`);
              }
            }
          } catch (error) {
            message.error(`附件上传失败（发票 #${group.invoiceId}）`);
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
      setNewOptions((prev) => prev.filter((o) => o.id !== inv.id));
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

  const renderFileRow = (uid: string, name: string, size: number, extra: React.ReactNode, onRemove: () => void) => (
    <div key={uid} className={styles.fileRow}>
      <PaperClipOutlined style={{ color: 'var(--muted-foreground)', fontSize: 12 }} />
      <span className={styles.fileName}>{name}</span>
      {extra}
      <span className={styles.fileSize}>{formatSize(size)}</span>
      <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={onRemove} />
    </div>
  );

  return (
    <Modal
      title="发票上传"
      open={open}
      onCancel={onClose}
      width={960}
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
          renderFileRow(p.uid, p.file.name, p.file.size, null, () =>
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
            row.file.size,
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
                style={{ width: 220 }}
                placeholder="选择关联发票"
                value={row.targetInvoiceId}
                onChange={(targetInvoiceId) => setAttachmentRows((prev) =>
                  prev.map((r) => (r.uid === row.uid ? { ...r, targetInvoiceId } : r))
                )}
                optionFilterProp="label"
                options={invoiceOptions}
              />
            </>,
            () => setAttachmentRows((prev) => prev.filter((r) => r.uid !== row.uid))
          )
        )}
      </div>
    </Modal>
  );
}
