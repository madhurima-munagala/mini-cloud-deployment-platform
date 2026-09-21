const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {})
    },
    ...options
  })

  const text = await response.text()
  let body = null
  try { body = text ? JSON.parse(text) : null } catch { body = { raw: text } }

  if (!response.ok) {
    const error = new Error(body?.error?.message || body?.message || `Request failed (${response.status})`)
    error.status = response.status
    error.code = body?.error?.code
    error.details = body?.error?.details
    throw error
  }
  return body
}

export const api = {
  me: () => request('/api/v1/auth/me'),
  logout: () => request('/api/v1/auth/logout', { method: 'POST' }),
  repositories: (page = 1, perPage = 100) => request(`/api/v1/repositories?page=${page}&per_page=${perPage}`),
  repositoryEnv: (id) => request(`/api/v1/repositories/${id}/env`),
  saveRepositoryEnv: (id, envVars) => request(`/api/v1/repositories/${id}/env`, {
    method: 'PUT', body: JSON.stringify({ env_vars: envVars })
  }),
  deployments: (params = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => value !== undefined && value !== '' && q.set(key, value))
    return request(`/api/v1/deployments${q.toString() ? `?${q}` : ''}`)
  },
  deployment: (id) => request(`/api/v1/deployments/${id}`),
  createDeployment: (payload) => request('/api/v1/deployments', {
    method: 'POST', body: JSON.stringify(payload)
  }),
  logs: (id, limit = 300) => request(`/api/v1/deployments/${id}/logs?limit=${limit}`),
  metrics: (id, limit = 60) => request(`/api/v1/deployments/${id}/metrics?limit=${limit}`),
  health: () => request('/api/v1/health')
}

export const githubLoginUrl = `${API_BASE}/api/v1/auth/github/login`
