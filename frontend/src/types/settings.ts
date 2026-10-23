export type ProviderProtocol = 'openai_compatible' | 'anthropic_compatible' | 'google';

export interface LlmProvider {
  id: number;
  name: string;
  display_name: string;
  protocol: ProviderProtocol;
  base_url: string | null;
  model: string | null;
  vision: boolean;
  api_mode: string; // 'chat' | 'responses' (openai_compatible only)
  temperature: number | null;
  max_output_tokens: number | null;
  is_preset: boolean;
  enabled: boolean;
  api_key_masked: string | null;
  has_api_key: boolean;
}

export interface LlmProviderCreateRequest {
  name: string;
  display_name: string;
  protocol: ProviderProtocol;
  base_url?: string;
  api_key?: string;
  model?: string;
  vision: boolean;
  api_mode?: string;
  temperature?: number | null;
  max_output_tokens?: number | null;
}

export interface LlmProviderUpdateRequest {
  display_name?: string;
  base_url?: string;
  api_key?: string;
  model?: string;
  vision?: boolean;
  enabled?: boolean;
  api_mode?: string;
  temperature?: number | null;
  max_output_tokens?: number | null;
}

export interface ProviderTestResponse {
  success: boolean;
  message: string;
  response_time_ms?: number;
}

export interface LLMStatusResponse {
  is_configured: boolean;
  active_provider: LlmProvider | null;
}

export interface AppSettings {
  ocr_engine: string;
  paddle_ocr_endpoint: string | null;
  baidu_ocr_api_key_masked: string | null;
  has_baidu_ocr_api_key: boolean;
  baidu_ocr_secret_key_masked: string | null;
  has_baidu_ocr_secret_key: boolean;
  mcp_enabled: boolean;
  mcp_token_masked: string | null;
  has_mcp_token: boolean;
}

export interface AppSettingsUpdateRequest {
  ocr_engine?: string;
  paddle_ocr_endpoint?: string;
  baidu_ocr_api_key?: string;
  baidu_ocr_secret_key?: string;
  mcp_enabled?: boolean;
  mcp_token?: string;
}
