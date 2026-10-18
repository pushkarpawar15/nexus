import { useState } from 'react';
import {
  Activity,
  BriefcaseBusiness,
  ChevronDown,
  Command,
  ClipboardList,
  GitBranch,
  Menu,
  Plus,
  ShieldCheck,
  Sparkles,
  Workflow,
  X,
} from 'lucide-react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/authState';
import { AuthDialog } from './AuthDialog';
import { CreateTaskDialog } from './CreateTaskDialog';
import { Button } from './ui';
const links = [
  { to: '/', label: 'Command Center', icon: Command },
  { to: '/board', label: 'Workforce Board', icon: Workflow },
  { to: '/tasks', label: 'Tasks', icon: ClipboardList },
  { to: '/activity', label: 'Agent Activity', icon: Activity },
  { to: '/approvals', label: 'Approvals', icon: ShieldCheck },
  { to: '/repositories', label: 'Repositories', icon: GitBranch },
  { to: '/skills', label: 'Skills', icon: Sparkles },
];
export function AppShell() {
  const { user, loading, configured, emulator, signOutUser } = useAuth();
  const location = useLocation();
  const [drawer, setDrawer] = useState(false);
  const [create, setCreate] = useState(false);
  const [auth, setAuth] = useState(false);
  const [account, setAccount] = useState(false);
  const current =
    links.find((item) => item.to === location.pathname)?.label ||
    (location.pathname.startsWith('/tasks/')
      ? 'Task detail'
      : location.pathname === '/repositories'
        ? 'Repositories'
        : location.pathname === '/skills'
          ? 'Skills'
          : 'Command Center');
  return (
    <div className="min-h-screen bg-canvas text-slate-800">
      {drawer && (
        <button
          aria-label="Close navigation"
          className="fixed inset-0 z-40 bg-black/40 lg:hidden"
          onClick={() => setDrawer(false)}
        />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-line bg-panel px-4 py-5 transition-transform lg:translate-x-0 ${drawer ? 'translate-x-0' : '-translate-x-full'}`}
      >
        <div className="flex items-center gap-3 px-2">
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-primary font-display font-bold text-white">
            N
          </span>
          <span className="font-display text-lg font-bold tracking-[.22em]">
            NEXUS
          </span>
          <button
            aria-label="Close navigation"
            className="ml-auto lg:hidden"
            onClick={() => setDrawer(false)}
          >
            <X size={18} />
          </button>
        </div>
        <p className="eyebrow mb-3 mt-10 px-3">Workspace</p>
        <nav className="grid gap-1">
          {links.map(({ to, label, icon: Icon }) => (
            <NavLink
              end={to === '/'}
              key={to}
              to={to}
              onClick={() => setDrawer(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl border px-3 py-3 text-sm transition ${isActive ? 'border-primary/20 bg-primary/10 text-primary' : 'border-transparent text-slate-500 hover:bg-elevated hover:text-slate-900'}`
              }
            >
              <Icon size={17} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-9 rounded-2xl border border-line bg-elevated p-4">
          <p className="text-xs font-bold text-slate-800">
            Operating principle
          </p>
          <p className="mt-2 font-display text-lg font-semibold">
            Human judgment stays in the loop.
          </p>
          <p className="mt-2 text-xs leading-5 text-muted">
            Every worker, decision, and handoff is visible.
          </p>
        </div>
        <div className="mt-auto border-t border-line px-3 pt-4 text-xs leading-5 text-muted">
          Engineering workspace
          <br />
          AI + human engineering operations
        </div>
      </aside>
      <div className="lg:pl-64">
        <header className="sticky top-0 z-30 flex h-20 items-center justify-between border-b border-line bg-panel/95 px-4 backdrop-blur sm:px-8">
          <div className="flex items-center gap-3">
            <button
              aria-label="Open navigation"
              className="lg:hidden"
              onClick={() => setDrawer(true)}
            >
              <Menu size={20} />
            </button>
            <div>
              <p className="text-[11px] text-muted">NEXUS / WORKSPACE</p>
              <p className="font-semibold">{current}</p>
            </div>
            <span className="hidden rounded-full border border-line px-3 py-1 text-xs text-muted xl:inline-flex">
              <BriefcaseBusiness size={13} className="mr-2" /> NEXUS workspace
            </span>
            {emulator && (
              <span className="hidden rounded-full border border-cyan-300 px-2 py-1 text-[10px] text-cyan-700 sm:inline-flex">
                Auth emulator
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              className="hidden sm:inline-flex"
              onClick={() => (user ? setCreate(true) : setAuth(true))}
            >
              <Plus size={16} />
              {user ? 'New task' : 'Sign in'}
            </Button>
            <div className="relative">
              <button
                aria-label="Account menu"
                className="grid h-10 w-10 place-items-center rounded-full border border-line bg-elevated font-semibold"
                onClick={() => (user ? setAccount(!account) : setAuth(true))}
              >
                {user ? (user.email?.[0] || 'U').toUpperCase() : '?'}
              </button>
              {account && user && (
                <div className="absolute right-0 mt-2 w-56 rounded-xl border border-line bg-panel p-2 shadow-xl">
                  <p className="truncate px-3 py-2 text-xs text-muted">
                    {user.email}
                  </p>
                  <button
                    className="w-full rounded-lg px-3 py-2 text-left text-sm hover:bg-elevated"
                    onClick={() =>
                      void signOutUser().then(() => setAccount(false))
                    }
                  >
                    Sign out{' '}
                    <ChevronDown size={14} className="inline rotate-180" />
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>
        <main className="mx-auto min-h-[calc(100vh-80px)] max-w-[1700px] px-4 py-8 sm:px-8 lg:px-10">
          {loading ? (
            <div className="grid min-h-[65vh] place-items-center text-sm text-muted">
              Checking your identity…
            </div>
          ) : user ? (
            <Outlet context={{ openCreate: () => setCreate(true) }} />
          ) : (
            <div className="grid min-h-[65vh] place-items-center">
              <div className="max-w-xl rounded-3xl border border-line bg-panel p-8 text-center sm:p-12">
                <p className="eyebrow">ENGINEERING WORKFORCE</p>
                <h1 className="mt-3 font-display text-3xl font-bold">
                  {configured
                    ? 'Your workspace is ready.'
                    : 'Connect your identity provider.'}
                </h1>
                <p className="mt-4 text-sm leading-6 text-muted">
                  {configured
                    ? 'Sign in to review work, activity, and decisions across your engineering workforce.'
                    : 'Configure the public Firebase web settings to enable authenticated task data.'}
                </p>
                <Button className="mt-6" onClick={() => setAuth(true)}>
                  {configured ? 'Sign in' : 'Configure Firebase'}
                </Button>
              </div>
            </div>
          )}
        </main>
      </div>
      <CreateTaskDialog open={create} onClose={() => setCreate(false)} />
      <AuthDialog open={auth} onClose={() => setAuth(false)} />
    </div>
  );
}
