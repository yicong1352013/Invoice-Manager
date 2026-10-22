import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Upload, message, List, Tag, Radio, Select, Table, Button } from 'antd';
import { UploadOutlined, CheckCircleOutlined, CloseCircleOutlined, PaperClipOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { listInvoices, uploadAttachments, uploadInvoices } from '../services/api';
import type { UploadResponse } from '../types/invoice';
import type { AttachmentCategory } from '../types/attachment';
import { ATTACHMENT_CATEGORY_LABELS } from '../types/attachment';
import styles from './UploadPage.module.css';

const { Dragger } = Upload;

const INVOICE_EXTS = ['pdf', 'jpg', 'jpeg', 'png'];
const ATTACHMENT_EXTS = [...INVOICE_EXTS, 'doc', 'docx', 'xls', 'xlsx'];

type FileKind = 'invoice' | 'attachment';

interface PickedFile {
  uid: string;
  file: File;
  kind: FileKind;
  category: AttachmentCategory;
  /** target invoice id for attachments, chosen in the assign stage */
  targetInvoiceId?: number;
}

interface AssignRow {
  uid: string;
  name: string;
  category: AttachmentCategory;
  file: File;
  targetInvoiceId?: number;
}

interface InvoiceOption {
  id: number;
  label: string;
}

function UploadPage() {
  const navigate = useNavigate();
  const [picked, setPicked] = useState<PickedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [results, setResults] = useState<UploadResponse[]>([]);
  const [assignRows, setAssignRows] = useState<AssignRow[] | null>(null);
  const [existingInvoices, setExistingInvoices] = useState<InvoiceOption[]>([]);

  const loadExistingInvoices = async () => {
    try {
      const data = await listInvoices({ page: 1, page_size: 100 });
      setExistingInvoices(data.items.map((inv) => ({
        id: inv.id,
        label: `#${inv.id} ${inv.invoice_number || inv.file_name}`,
      })));
    } catch {
      // non-fatal: new-batch invoices are still assignable
    }
  };

  useEffect(() => {
    if (assignRows !== null) {
      loadExistingInvoices();
    }
  }, [assignRows]);

  const invoiceFiles = useMemo(() => picked.filter((p) => p.kind === 'invoice'), [picked]);
  const attachmentFiles = useMemo(() => picked.filter((p) => p.kind === 'attachment'), [picked]);

  const handleUpload = async () => {
    if (picked.length === 0) {
      message.warning('请先选择文件');
      return;
    }

    setUploading(true);
    try {
      let newInvoices: InvoiceOption[] = [];
      const allResults: UploadResponse[] = [];

      if (invoiceFiles.length > 0) {
        const response = await uploadInvoices(invoiceFiles.map((p) => p.file));
        allResults.push(...response);
        newInvoices = response
          .filter((r) => r.status === 'success' && r.id > 0)
          .map((r) => ({ id: r.id, label: `#${r.id} ${r.file_name}（本次上传）` }));
      }

      if (attachmentFiles.length > 0) {
        // Move to the assign stage: choose which invoice each attachment belongs to
        setAssignRows(attachmentFiles.map((p) => ({
          uid: p.uid,
          name: p.file.name,
          category: p.category,
          file: p.file,
          targetInvoiceId: newInvoices[0]?.id,
        })));
        setNewInvoiceOptions(newInvoices);
        const preview: UploadResponse[] = invoiceFiles.map((p) => ({
          id: 0, file_name: p.file.name, status: 'success', message: '已上传，等待附件关联',
        }));
        setResults(allResults.length > 0 ? allResults : preview);
      } else {
        setResults(allResults);
        const successCount = allResults.filter((r) => r.status === 'success').length;
        if (successCount > 0) {
          message.success(`成功上传 ${successCount} 个文件`);
        }
      }
      setPicked([]);
    } catch (error) {
      message.error('上传失败，请重试');
      console.error(error);
    } finally {
      setUploading(false);
    }
  };

  // Options for the target invoice select: new batch first, then existing
  const [newInvoiceOptions, setNewInvoiceOptions] = useState<InvoiceOption[]>([]);
  const invoiceOptions = useMemo(
    () => [...newInvoiceOptions, ...existingInvoices.filter(
      (e) => !newInvoiceOptions.some((n) => n.id === e.id)
    )],
    [newInvoiceOptions, existingInvoices]
  );

  const handleConfirmAttachments = async () => {
    if (!assignRows) return;
    const unassigned = assignRows.filter((r) => !r.targetInvoiceId);
    if (unassigned.length > 0) {
      message.warning('请为每个附件选择关联的发票');
      return;
    }

    setUploading(true);
    try {
      // Group by (target invoice, category) to batch the uploads
      const groups = new Map<string, { invoiceId: number; category: AttachmentCategory; files: File[] }>();
      for (const row of assignRows) {
        const key = `${row.targetInvoiceId}:${row.category}`;
        if (!groups.has(key)) {
          groups.set(key, { invoiceId: row.targetInvoiceId!, category: row.category, files: [] });
        }
        groups.get(key)!.files.push(row.file);
      }

      const attachResults: UploadResponse[] = [];
      for (const group of groups.values()) {
        const response = await uploadAttachments(group.invoiceId, group.files, group.category);
        attachResults.push(...response);
      }

      setResults((prev) => [...prev.filter((r) => r.message !== '已上传，等待附件关联'), ...attachResults]);
      const successCount = attachResults.filter((r) => r.status === 'success').length;
      message.success(`已上传 ${successCount} 个附件`);
      setAssignRows(null);
      setNewInvoiceOptions([]);
    } catch (error) {
      message.error('附件上传失败，请重试');
      console.error(error);
    } finally {
      setUploading(false);
    }
  };

  const uploadProps = {
    multiple: true,
    accept: ATTACHMENT_EXTS.map((ext) => `.${ext}`).join(','),
    beforeUpload: (file: File) => {
      const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
      if (!ATTACHMENT_EXTS.includes(ext)) {
        message.error('不支持的文件类型');
        return Upload.LIST_IGNORE;
      }
      if (file.size / 1024 / 1024 >= 10) {
        message.error('文件大小不能超过 10MB');
        return Upload.LIST_IGNORE;
      }
      setPicked((prev) => [...prev, {
        uid: `${file.name}-${file.size}-${Date.now()}`,
        file,
        // documents that are not invoice formats default to attachments
        kind: INVOICE_EXTS.includes(ext) ? 'invoice' : 'attachment',
        category: 'other',
      }]);
      return Upload.LIST_IGNORE; // managed manually below
    },
  };

  const kindColumns: ColumnsType<PickedFile> = [
    {
      title: '文件',
      dataIndex: 'name',
      render: (_, record) => (
        <span className={styles.fileNameCell}>
          <PaperClipOutlined style={{ marginRight: 6, color: 'var(--muted-foreground)' }} />
          {record.file.name}
        </span>
      ),
    },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 180,
      render: (_, record) => (
        <Radio.Group
          size="small"
          value={record.kind}
          onChange={(e) => setPicked((prev) => prev.map((p) =>
            p.uid === record.uid ? { ...p, kind: e.target.value } : p
          ))}
        >
          <Radio.Button value="invoice">发票</Radio.Button>
          <Radio.Button value="attachment">附件</Radio.Button>
        </Radio.Group>
      ),
    },
    {
      title: '附件分类',
      dataIndex: 'category',
      width: 140,
      render: (_, record) => record.kind === 'attachment' ? (
        <Select
          size="small"
          value={record.category}
          style={{ width: 110 }}
          onChange={(value) => setPicked((prev) => prev.map((p) =>
            p.uid === record.uid ? { ...p, category: value } : p
          ))}
          options={(Object.keys(ATTACHMENT_CATEGORY_LABELS) as AttachmentCategory[]).map((c) => ({
            value: c, label: ATTACHMENT_CATEGORY_LABELS[c],
          }))}
        />
      ) : (
        <span style={{ color: 'var(--muted-foreground)' }}>—</span>
      ),
    },
    {
      title: '',
      width: 70,
      render: (_, record) => (
        <Button
          size="small"
          type="text"
          danger
          onClick={() => setPicked((prev) => prev.filter((p) => p.uid !== record.uid))}
        >
          移除
        </Button>
      ),
    },
  ];

  return (
    <div className={styles.pageContainer}>
      <div className={styles.contentWrapper}>
        <header className={styles.pageHeader}>
          <h1 className={styles.pageTitle}>上传发票</h1>
        </header>

        <div className={styles.uploadCard}>
          <div className={styles.uploadDropzone}>
            <Dragger {...uploadProps} showUploadList={false}>
              <div className="ant-upload-drag-icon">
                <span className={styles.uploadIcon}>
                  <UploadOutlined />
                </span>
              </div>
              <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
              <p className="ant-upload-hint">
                发票支持 PDF、JPG、PNG；附件另支持 DOC(X)、XLS(X)。可批量混传，上传后标记类型
              </p>
            </Dragger>
          </div>

          {picked.length > 0 && (
            <Table<PickedFile>
              rowKey="uid"
              size="small"
              columns={kindColumns}
              dataSource={picked}
              pagination={false}
              style={{ marginTop: 16 }}
            />
          )}

          <div className={styles.actionButtons}>
            <button
              className={styles.uploadButton}
              onClick={handleUpload}
              disabled={picked.length === 0 || uploading}
            >
              {uploading ? '上传中...' : '开始上传'}
            </button>
            <button
              className={styles.backButton}
              onClick={() => navigate('/')}
            >
              返回列表
            </button>
          </div>
        </div>

        {assignRows !== null && (
          <div className={styles.resultsCard}>
            <h2 className={styles.resultsTitle}>关联附件到发票</h2>
            <p style={{ color: 'var(--muted-foreground)', fontSize: 13 }}>
              为每个附件选择所属发票（可搜索发票号码或文件名），然后确认上传
            </p>
            {assignRows.map((row) => (
              <div key={row.uid} className={styles.assignRow}>
                <span className={styles.fileNameCell}>{row.name}</span>
                <Select
                  size="small"
                  style={{ width: 140 }}
                  value={row.category}
                  onChange={(category) => setAssignRows((prev) =>
                    prev ? prev.map((r) => (r.uid === row.uid ? { ...r, category } : r)) : prev
                  )}
                  options={(Object.keys(ATTACHMENT_CATEGORY_LABELS) as AttachmentCategory[]).map((c) => ({
                    value: c, label: ATTACHMENT_CATEGORY_LABELS[c],
                  }))}
                />
                <Select
                  size="small"
                  showSearch
                  style={{ minWidth: 260, flex: 1 }}
                  placeholder="选择关联的发票"
                  value={row.targetInvoiceId}
                  onChange={(targetInvoiceId) => setAssignRows((prev) =>
                    prev ? prev.map((r) => (r.uid === row.uid ? { ...r, targetInvoiceId } : r)) : prev
                  )}
                  optionFilterProp="label"
                  options={invoiceOptions}
                />
              </div>
            ))}
            <button
              className={styles.uploadButton}
              style={{ marginTop: 12 }}
              onClick={handleConfirmAttachments}
              disabled={uploading}
            >
              {uploading ? '上传中...' : '确认上传附件'}
            </button>
          </div>
        )}

        {results.length > 0 && (
          <div className={styles.resultsCard}>
            <h2 className={styles.resultsTitle}>上传结果</h2>
            <List
              className={styles.resultsList}
              dataSource={results}
              renderItem={(item) => (
                <List.Item>
                  <List.Item.Meta
                    avatar={
                      item.status === 'success' ? (
                        <CheckCircleOutlined style={{ color: '#52c41a', fontSize: 24 }} />
                      ) : (
                        <CloseCircleOutlined style={{ color: '#ff4d4f', fontSize: 24 }} />
                      )
                    }
                    title={item.file_name}
                    description={item.message}
                  />
                  {item.status === 'success' && item.id > 0 && (
                    <Tag color="success">ID: {item.id}</Tag>
                  )}
                </List.Item>
              )}
            />
            <button
              className={styles.viewListButton}
              onClick={() => navigate('/')}
            >
              查看发票列表
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default UploadPage;
