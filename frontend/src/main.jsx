import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, githubLoginUrl } from './api'
import './styles.css'

const STATUS_META = {
  pending: ['Pending', 'yellow'], building: ['Building', 'blue'], starting: ['Starting', 'blue'],
  running: ['Live', 'green'], failed: ['Failed', 'red'], stopped: ['Stopped', 'gray']
}

function Icon({ name, size = 18 }) {
  const paths = {
    grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
    git: 'M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7c-2.78.61-3.37-1.18-3.37-1.18-.46-1.16-1.11-1.47-1.11-1.47-.91-.63.07-.62.07-.62 1 .07 1.53 1.03 1.53 1.03.9 1.53 2.36 1.09 2.94.83.09-.65.35-1.09.64-1.34-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02a9.6 9.6 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.69-4.57 4.94.36.31.68.92.68 1.85v2.75c0 .26.18.57.69.47A10 10 0 0 0 12 2Z',
    rocket: 'M14.5 3.5C17 1 21 1 21 1s0 4-2.5 6.5c-1.15 1.15-2.42 1.77-3.55 1.93l-3.38 3.38M8.5 15.5 5 19l-2 0 0-2 3.5-3.5M12 12l-2.5-2.5M9 20l-2-2M4 13l-1-1 3-3 2 1',
    activity: 'M3 12h4l2-7 4 14 2-7h6',
    settings: 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-1.7 1.7-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V20h-2.4v-.2a1.7 1.7 0 0 0-1.03-1.56 1.7 1.7 0 0 0-1.88.34l-.06.06-1.7-1.7.06-.06A1.7 1.7 0 0 0 8.4 15a1.7 1.7 0 0 0-1.56-1.03H6v-2.4h.84A1.7 1.7 0 0 0 8.4 10a1.7 1.7 0 0 0-.34-1.88L8 8.06l1.7-1.7.06.06A1.7 1.7 0 0 0 11.64 6.1 1.7 1.7 0 0 0 12.67 4.5V4h2.4v.5a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 1.7 1.7-.06.06A1.7 1.7 0 0 0 19.34 9a1.7 1.7 0 0 0 1.56 1.03h.6v2.4h-.6A1.7 1.7 0 0 0 19.4 15Z',
    logout: 'M10 17l5-5-5-5M15 12H3M21 19V5a2 2 0 0 0-2-2h-5',
    plus: 'M12 5v14M5 12h14',
    arrow: 'M5 12h14M13 6l6 6-6 6',
    refresh: 'M20 11a8 8 0 0 0-14.9-4L3 10M4 5v5h5M4 13a8 8 0 0 0 14.9 4L21 14m0 5v-5h-5',
    external: 'M14 4h6v6M20 4l-9 9M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5',
    terminal: 'M5 7l5 5-5 5M12 17h7',
    key: 'M21 2l-2 2m-7.5 7.5a5 5 0 1 1-7-7 5 5 0 0 1 7 7ZM15 7l3 3m-1-4 2 2',
    menu: 'M4 6h16M4 12h16M4 18h16',
    close: 'M6 6l12 12M18 6 6 18',
    check: 'm5 12 4 4L19 6',
    clock: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
    alert: 'M12 9v4M12 17h.01M10.3 3.6 2.2 18a2 2 0 0 0 1.74 3h16.12A2 2 0 0 0 21.8 18L13.7 3.6a2 2 0 0 0-3.4 0Z'
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name] || paths.grid} /></svg>
}

function StatusBadge({ status }) {
  const [label, tone] = STATUS_META[status] || [status || 'Unknown', 'gray']
  return <span className={`status ${tone}`}><span className="status-dot" />{label}</span>
}

function Loading({ text = 'Loading…' }) { return <div className="loading"><span className="spinner" />{text}</div> }
function ErrorBox({ error, onRetry }) { return <div className="error-box"><Icon name="alert" /><div><strong>Something went wrong</strong><p>{error?.message || 'Unable to load this data.'}</p>{onRetry && <button className="btn btn-secondary btn-sm" onClick={onRetry}>Try again</button>}</div></div> }

function AppShell({ user, onLogout, children }) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const nav = [
    ['/dashboard', 'Overview', 'grid'], ['/repositories', 'Repositories', 'git'], ['/deployments', 'Deployments', 'rocket']
  ]
  return <div className="app-shell">
    <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}>
      <div className="brand"><div className="brand-mark"><Icon name="rocket" size={20} /></div><span>CloudForge</span></div>
      <div className="workspace"><span className="workspace-dot" /> Personal workspace</div>
      <nav className="nav-list">
        <div className="nav-label">Workspace</div>
        {nav.map(([to, label, icon]) => <NavLink key={to} to={to} onClick={() => setMobileOpen(false)} className={({isActive}) => `nav-item ${isActive ? 'active' : ''}`}><Icon name={icon}/><span>{label}</span></NavLink>)}
      </nav>
      <div className="sidebar-bottom">
        <div className="status-card"><span className="pulse"/><div><strong>Platform online</strong><small>API connected</small></div></div>
        <button className="nav-item ghost" onClick={onLogout}><Icon name="logout"/><span>Sign out</span></button>
      </div>
    </aside>
    {mobileOpen && <button className="mobile-backdrop" onClick={() => setMobileOpen(false)} aria-label="Close menu" />}
    <main className="main">
      <header className="topbar"><button className="mobile-menu" onClick={() => setMobileOpen(true)}><Icon name="menu"/></button><div className="topbar-spacer"/><div className="user-menu"><img src={user?.avatar_url || `https://ui-avatars.com/api/?name=${encodeURIComponent(user?.username || 'U')}&background=111827&color=fff`} /><span>{user?.username || 'Developer'}</span></div></header>
      <div className="page-content">{children}</div>
    </main>
  </div>
}

function Login() {
  const location = useLocation()
  const params = new URLSearchParams(location.search)
  const failed = params.get('error') === 'oauth_failed'
  return <div className="login-page">
    <div className="login-glow glow-a"/><div className="login-glow glow-b"/>
    <div className="login-card">
      <div className="brand brand-center"><div className="brand-mark"><Icon name="rocket" size={22}/></div><span>CloudForge</span></div>
      <div className="login-icon"><Icon name="git" size={28}/></div>
      <h1>Deploy without the busywork.</h1>
      <p>Connect your GitHub repositories, deploy in seconds, and watch every build from one clean dashboard.</p>
      {failed && <div className="alert alert-error"><Icon name="alert"/> GitHub sign-in failed. Please try again.</div>}
      <a className="github-btn" href={githubLoginUrl}><Icon name="git" size={19}/> Continue with GitHub <Icon name="arrow" size={17}/></a>
      <div className="login-points"><span><Icon name="check"/> Cookie-based secure sessions</span><span><Icon name="check"/> Live deployment logs</span><span><Icon name="check"/> CPU & memory monitoring</span></div>
    </div>
    <p className="login-footer">Mini Cloud Deployment Platform · Vercel/Heroku-style project</p>
  </div>
}

function Dashboard() {
  const [repos, setRepos] = useState([]); const [deployments, setDeployments] = useState([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(null)
  const load = async () => { setLoading(true); setError(null); try { const [r,d] = await Promise.all([api.repositories(), api.deployments({per_page: 100})]); setRepos(r.data || []); setDeployments(d.data || []) } catch(e) { setError(e) } finally { setLoading(false) } }
  useEffect(() => { load() }, [])
  const running = deployments.filter(d => d.status === 'running').length
  const active = deployments.filter(d => ['pending','building','starting'].includes(d.status)).length
  return <>
    <PageHeader title="Overview" subtitle="Your deployment workspace at a glance." action={<Link to="/repositories" className="btn btn-primary"><Icon name="plus"/> New deployment</Link>}/>
    {error ? <ErrorBox error={error} onRetry={load}/> : loading ? <Loading/> : <>
      <section className="stat-grid"><StatCard label="Repositories" value={repos.length} icon="git" hint="Synced from GitHub"/><StatCard label="Live deployments" value={running} icon="activity" hint="Currently running"/><StatCard label="In progress" value={active} icon="clock" hint="Building or starting"/><StatCard label="Total deployments" value={deployments.length} icon="rocket" hint="All-time history"/></section>
      <div className="two-col">
        <section className="panel"><PanelHeader title="Recent deployments" link="/deployments"/><DeploymentTable rows={deployments.slice(0,6)} empty="No deployments yet."/></section>
        <section className="panel"><PanelHeader title="Your repositories" link="/repositories"/><div className="repo-mini-list">{repos.slice(0,6).map(r => <Link className="repo-mini" to={`/repositories/${r.id}`} key={r.id}><div className="repo-avatar"><Icon name="git" size={17}/></div><div><strong>{r.name}</strong><small>{r.private ? 'Private' : 'Public'} · {r.default_branch}</small></div><Icon name="arrow" size={16}/></Link>)}{repos.length === 0 && <EmptyState text="No repositories found."/>}</div></section>
      </div>
    </>}
  </>
}

function StatCard({label,value,icon,hint}) { return <div className="stat-card"><div className="stat-icon"><Icon name={icon}/></div><div className="stat-copy"><span>{label}</span><strong>{value}</strong><small>{hint}</small></div></div> }
function PageHeader({title,subtitle,action}) { return <div className="page-header"><div><h1>{title}</h1><p>{subtitle}</p></div>{action}</div> }
function PanelHeader({title,link}) { return <div className="panel-header"><h2>{title}</h2>{link && <Link to={link}>View all <Icon name="arrow" size={15}/></Link>}</div> }
function EmptyState({text}) { return <div className="empty"><div className="empty-icon"><Icon name="grid"/></div><p>{text}</p></div> }
function DeploymentTable({rows,empty='No deployments found.'}) { return rows.length ? <div className="table-wrap"><table><thead><tr><th>Repository</th><th>Branch</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>{rows.map(d => <tr key={d.id}><td><Link className="table-link" to={`/deployments/${d.id}`}>{d.repository_name}</Link></td><td><code>{d.branch}</code></td><td><StatusBadge status={d.status}/></td><td>{formatDate(d.created_at)}</td><td><Link className="icon-btn" to={`/deployments/${d.id}`}><Icon name="arrow" size={16}/></Link></td></tr>)}</tbody></table></div> : <EmptyState text={empty}/>
}

function Repositories() {
  const [repos,setRepos]=useState([]), [loading,setLoading]=useState(true), [error,setError]=useState(null), [search,setSearch]=useState('')
  const load=async()=>{setLoading(true);setError(null);try{const r=await api.repositories();setRepos(r.data||[])}catch(e){setError(e)}finally{setLoading(false)}}
  useEffect(()=>{load()},[])
  const filtered=useMemo(()=>repos.filter(r=>`${r.name} ${r.full_name}`.toLowerCase().includes(search.toLowerCase())),[repos,search])
  return <><PageHeader title="Repositories" subtitle="GitHub repositories available for deployment." action={<button className="btn btn-secondary" onClick={load}><Icon name="refresh"/> Sync GitHub</button>}/>{error?<ErrorBox error={error} onRetry={load}/>:loading?<Loading text="Syncing GitHub repositories…"/>:<section className="panel"><div className="toolbar"><div className="search"><span>⌕</span><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search repositories…"/></div><span className="muted">{filtered.length} repositories</span></div><div className="repo-grid">{filtered.map(r=><RepositoryCard repo={r} key={r.id}/>)}{!filtered.length&&<EmptyState text="No matching repositories."/>}</div></section>}</>
}
function RepositoryCard({repo}) { return <div className="repo-card"><div className="repo-card-top"><div className="repo-avatar large"><Icon name="git"/></div><span className={`visibility ${repo.private?'private':'public'}`}>{repo.private?'Private':'Public'}</span></div><h3>{repo.name}</h3><p>{repo.full_name}</p><div className="repo-meta"><span><Icon name="git" size={14}/>{repo.default_branch}</span><span>Updated {formatDate(repo.github_updated_at)}</span></div><div className="repo-actions"><Link className="btn btn-secondary btn-sm" to={`/repositories/${repo.id}`}>Settings</Link><Link className="btn btn-primary btn-sm" to={`/repositories/${repo.id}/deploy`}>Deploy <Icon name="arrow" size={14}/></Link></div></div> }

function RepositoryPage() {
  const {id}=useParams(); const navigate=useNavigate(); const [repos,setRepos]=useState([]); const [env,setEnv]=useState([]); const [loading,setLoading]=useState(true); const [saving,setSaving]=useState(false); const [message,setMessage]=useState(null); const [error,setError]=useState(null)
  const repo=useMemo(()=>repos.find(r=>String(r.id)===String(id)),[repos,id])
  const load=async()=>{setLoading(true);try{const r=await api.repositories();setRepos(r.data||[]);const e=await api.repositoryEnv(id);setEnv((e.data||[]).map(x=>({key:x.key,value:''})))}catch(e){setError(e)}finally{setLoading(false)}}
  useEffect(()=>{load()},[id])
  const save=async()=>{setSaving(true);setMessage(null);setError(null);try{const clean=env.filter(x=>x.key.trim()).map(x=>({key:x.key.trim(),value:x.value}));await api.saveRepositoryEnv(id,clean);setMessage('Environment variables saved. Values are masked after saving.')}catch(e){setError(e)}finally{setSaving(false)}}
  if(loading)return <Loading/>; if(error&&!repo)return <ErrorBox error={error} onRetry={load}/>; if(!repo)return <EmptyState text="Repository not found."/>
  return <><div className="breadcrumbs"><Link to="/repositories">Repositories</Link><span>/</span><span>{repo.name}</span></div><PageHeader title={repo.name} subtitle={repo.full_name} action={<Link to={`/repositories/${repo.id}/deploy`} className="btn btn-primary"><Icon name="rocket"/> Deploy</Link>}/><div className="detail-grid"><section className="panel"><PanelHeader title="Repository details"/><div className="detail-list"><Detail label="Visibility" value={repo.private?'Private':'Public'}/><Detail label="Default branch" value={repo.default_branch}/><Detail label="GitHub repo ID" value={repo.github_repo_id}/><Detail label="Last updated" value={formatDate(repo.github_updated_at)}/><Detail label="Clone URL" value={repo.clone_url} link/></div></section><section className="panel"><PanelHeader title="Environment variables"/><p className="panel-description">Values are never returned by the API. Leave the value blank only if you do not want to change that key.</p>{env.map((v,i)=><EnvRow key={i} item={v} onChange={x=>setEnv(env.map((a,j)=>j===i?x:a))} onRemove={()=>setEnv(env.filter((_,j)=>j!==i))}/>)}<button className="add-row" onClick={()=>setEnv([...env,{key:'',value:''}])}><Icon name="plus" size={16}/> Add variable</button>{message&&<div className="alert alert-success"><Icon name="check"/>{message}</div>}{error&&<div className="alert alert-error"><Icon name="alert"/>{error.message}</div>}<div className="save-row"><button className="btn btn-primary" onClick={save} disabled={saving}>{saving?'Saving…':'Save variables'}</button></div></section></div></>
}
function Detail({label,value,link}) { return <div className="detail"><span>{label}</span>{link?<a href={value} target="_blank" rel="noreferrer">{value}<Icon name="external" size={13}/></a>:<strong>{value||'—'}</strong>}</div> }
function EnvRow({item,onChange,onRemove}) { return <div className="env-row"><input value={item.key} onChange={e=>onChange({...item,key:e.target.value})} placeholder="VARIABLE_NAME"/><div className="secret-input"><Icon name="key" size={15}/><input type="password" value={item.value} onChange={e=>onChange({...item,value:e.target.value})} placeholder={item.value?'••••••••':'Value'}/></div><button className="icon-btn danger" onClick={onRemove}><Icon name="close" size={16}/></button></div> }

function Deploy() {
  const {id}=useParams(); const navigate=useNavigate(); const [repos,setRepos]=useState([]); const [branch,setBranch]=useState(''); const [env,setEnv]=useState([]); const [loading,setLoading]=useState(true); const [deploying,setDeploying]=useState(false); const [error,setError]=useState(null)
  useEffect(()=>{(async()=>{try{const r=await api.repositories();setRepos(r.data||[]);const repo=(r.data||[]).find(x=>String(x.id)===String(id));if(repo)setBranch(repo.default_branch)}catch(e){setError(e)}finally{setLoading(false)}})()},[id])
  const repo=repos.find(x=>String(x.id)===String(id))
  const submit=async()=>{if(!repo)return;setDeploying(true);setError(null);try{const payload={repository_id:repo.id,branch:branch||null};const clean=env.filter(x=>x.key.trim()).map(x=>({key:x.key.trim(),value:x.value}));if(clean.length)payload.env_vars=clean;const res=await api.createDeployment(payload);navigate(`/deployments/${res.data.id}`)}catch(e){setError(e)}finally{setDeploying(false)}}
  if(loading)return <Loading/>; if(error&&!repo)return <ErrorBox error={error}/>; if(!repo)return <EmptyState text="Repository not found."/>
  return <><div className="breadcrumbs"><Link to="/repositories">Repositories</Link><span>/</span><Link to={`/repositories/${id}`}>{repo.name}</Link><span>/</span><span>Deploy</span></div><PageHeader title={`Deploy ${repo.name}`} subtitle="Create a new deployment from a GitHub branch."/><div className="deploy-layout"><section className="panel form-panel"><label>Repository</label><div className="selected-repo"><div className="repo-avatar"><Icon name="git"/></div><div><strong>{repo.full_name}</strong><small>GitHub · {repo.private?'Private':'Public'}</small></div><Icon name="check" size={18}/></div><label>Branch</label><input className="input" value={branch} onChange={e=>setBranch(e.target.value)} placeholder="main"/><p className="field-help">The branch to clone and deploy.</p><div className="section-divider"/><div className="form-heading"><div><h3>Environment overrides</h3><p>Optional values for this deployment only.</p></div></div>{env.map((v,i)=><EnvRow key={i} item={v} onChange={x=>setEnv(env.map((a,j)=>j===i?x:a))} onRemove={()=>setEnv(env.filter((_,j)=>j!==i))}/>)}<button className="add-row" onClick={()=>setEnv([...env,{key:'',value:''}])}><Icon name="plus" size={16}/> Add variable</button>{error&&<div className="alert alert-error"><Icon name="alert"/>{error.message}</div>}<button className="btn btn-primary btn-large deploy-submit" disabled={deploying} onClick={submit}><Icon name="rocket"/>{deploying?'Creating deployment…':'Deploy now'}<Icon name="arrow"/></button></section><aside className="deploy-summary"><div className="summary-card"><span className="summary-label">DEPLOYMENT</span><h3>{repo.name}</h3><div className="summary-row"><span>Source</span><strong>{branch||repo.default_branch}</strong></div><div className="summary-row"><span>Environment</span><strong>{env.filter(x=>x.key.trim()).length ? `${env.filter(x=>x.key.trim()).length} variables` : 'None'}</strong></div><div className="summary-note"><Icon name="clock" size={16}/><span>Build progress and logs will appear after creation.</span></div></div></aside></div></>
}

function Deployments() { const [rows,setRows]=useState([]),[loading,setLoading]=useState(true),[error,setError]=useState(null),[filter,setFilter]=useState(''); const load=async()=>{setLoading(true);try{const r=await api.deployments({per_page:100,status:filter||undefined});setRows(r.data||[])}catch(e){setError(e)}finally{setLoading(false)}};useEffect(()=>{load()},[filter]);return <><PageHeader title="Deployments" subtitle="Track every build, release, and deployment event." action={<button className="btn btn-secondary" onClick={load}><Icon name="refresh"/> Refresh</button>}/>{error?<ErrorBox error={error} onRetry={load}/>:loading?<Loading/>:<section className="panel"><div className="toolbar"><div className="filter-pills">{['','pending','building','starting','running','failed','stopped'].map(s=><button key={s} className={filter===s?'selected':''} onClick={()=>setFilter(s)}>{s||'All'}</button>)}</div><span className="muted">{rows.length} deployments</span></div><DeploymentTable rows={rows}/></section>}</> }

function DeploymentDetail() { const {id}=useParams(); const [deployment,setDeployment]=useState(null),[logs,setLogs]=useState([]),[metrics,setMetrics]=useState(null),[error,setError]=useState(null),[loading,setLoading]=useState(true); const load=async()=>{try{const d=await api.deployment(id);setDeployment(d.data);const l=await api.logs(id);setLogs(l.data||[]);if(['running','failed','stopped'].includes(d.data.status)){try{const m=await api.metrics(id);setMetrics(m.data)}catch{}}}catch(e){setError(e)}finally{setLoading(false)}};useEffect(()=>{load()},[id]);useEffect(()=>{if(!deployment||['running','pending','building','starting'].includes(deployment.status)){const t=setInterval(load,4000);return()=>clearInterval(t)}},[deployment?.status]);if(loading)return <Loading/>;if(error)return <ErrorBox error={error} onRetry={load}/>;return <><div className="breadcrumbs"><Link to="/deployments">Deployments</Link><span>/</span><span>{deployment.repository_name}</span></div><PageHeader title={deployment.repository_name} subtitle={`${deployment.branch} · ${formatDate(deployment.created_at)}`} action={deployment.live_url?<a className="btn btn-primary" href={deployment.live_url} target="_blank" rel="noreferrer"><Icon name="external"/> Open live site</a>:<StatusBadge status={deployment.status}/>}/><section className="deployment-hero"><div className="deployment-status"><StatusBadge status={deployment.status}/><h2>{deployment.status==='running'?'Deployment is live':deployment.status==='failed'?'Deployment failed':`Deployment ${deployment.status}`}</h2><p>{deployment.error_message||'CloudForge is processing this deployment and will keep the status updated.'}</p></div><div className="deploy-facts"><div><span>Branch</span><strong>{deployment.branch}</strong></div><div><span>Created</span><strong>{formatDate(deployment.created_at)}</strong></div><div><span>Container</span><strong>{deployment.container_id||'—'}</strong></div></div></section><div className="two-col detail-sections"><section className="panel"><PanelHeader title="Build logs"/><div className="terminal">{logs.length?logs.map(l=><div className="log-line" key={l.id}><time>{formatTime(l.timestamp)}</time><span className={`log-level ${l.level?.toLowerCase()}`}>{l.level}</span><span>{l.message}</span></div>):<div className="terminal-empty">Waiting for deployment logs…</div>}</div></section><section className="panel"><PanelHeader title="Runtime metrics"/><Metrics metrics={metrics}/></section></div></> }
function Metrics({metrics}) { if(!metrics)return <div className="metrics-empty"><Icon name="activity" size={28}/><p>No monitoring data available yet.</p><small>Metrics appear once the deployment reports runtime data.</small></div>;const latest=metrics.latest;return <><div className="metric-cards"><div><span>CPU</span><strong>{Number(latest.cpu_percent).toFixed(1)}%</strong></div><div><span>Memory</span><strong>{Number(latest.memory_mb).toFixed(1)} MB</strong></div><div><span>Container</span><strong className="capitalize">{metrics.container_status}</strong></div></div><div className="sparkline"><div className="sparkline-label"><span>CPU history</span><small>{metrics.history?.length||0} points</small></div><MiniChart values={(metrics.history||[]).map(x=>x.cpu_percent)}/></div></> }
function MiniChart({values}) { if(!values.length)return null;const max=Math.max(...values,1),min=Math.min(...values,0),range=max-min||1;const points=values.map((v,i)=>`${(i/(Math.max(values.length-1,1))*100).toFixed(1)},${(100-(v-min)/range*82-9).toFixed(1)}`).join(' ');return <svg className="chart" viewBox="0 0 100 100" preserveAspectRatio="none"><polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke"/></svg> }

function App() { const [user,setUser]=useState(null);const [loading,setLoading]=useState(true);const [authError,setAuthError]=useState(false);useEffect(()=>{api.me().then(r=>setUser(r.data)).catch(()=>setAuthError(true)).finally(()=>setLoading(false))},[]);const logout=async()=>{try{await api.logout()}finally{window.location.href='/login'}};if(loading)return <div className="fullscreen-loading"><div className="brand"><div className="brand-mark"><Icon name="rocket"/></div><span>CloudForge</span></div><Loading text="Loading workspace…"/></div>;if(authError)return <Login/>;return <AppShell user={user} onLogout={logout}><Routes><Route path="/" element={<Dashboard/>}/><Route path="/dashboard" element={<Dashboard/>}/><Route path="/repositories" element={<Repositories/>}/><Route path="/repositories/:id" element={<RepositoryPage/>}/><Route path="/repositories/:id/deploy" element={<Deploy/>}/><Route path="/deployments" element={<Deployments/>}/><Route path="/deployments/:id" element={<DeploymentDetail/>}/><Route path="*" element={<Dashboard/>}/></Routes></AppShell> }

function formatDate(value){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}
function formatTime(value){if(!value)return '';const d=new Date(value);return Number.isNaN(d.getTime())?'':d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})}

createRoot(document.getElementById('root')).render(<BrowserRouter><App/></BrowserRouter>)
