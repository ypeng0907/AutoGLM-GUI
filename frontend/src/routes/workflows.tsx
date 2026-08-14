import { createFileRoute } from '@tanstack/react-router';
import { useState, useEffect, useRef } from 'react';
import {
  listWorkflows,
  createWorkflow,
  updateWorkflow,
  deleteWorkflow,
  runWorkflow,
  getDevices,
  streamTaskEvents,
  cancelTaskRun,
  type Workflow,
  type Device,
  type WorkflowRunTaskInfo,
  type TaskEventRecordResponse,
} from '../api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  Plus,
  Edit,
  Trash2,
  Loader2,
  ArrowUp,
  ArrowDown,
  Play,
  MessageSquare,
  CheckCircle,
  XCircle,
  Bot,
  ChevronLeft,
  Square,
} from 'lucide-react';
import { useTranslation } from '../lib/i18n-context';

export const Route = createFileRoute('/workflows')({
  component: WorkflowsComponent,
});

interface WorkflowStep {
  id: string;
  title: string;
  description: string;
}

const STEP_PREFIX_REGEX =
  /^(?:步骤\s*\d+\s*[:：.]?\s*|step\s*\d+\s*[:：.]?\s*|\d+\s*[.)、．]\s+|[-*]\s+)/i;
const DESCRIPTION_PREFIX_REGEX =
  /^(?:描述|说明|备注|验证(?:点|标准)?|校验(?:点)?|检查(?:点)?|断言|expected|assert(?:ion)?|verify|description|desc)\s*[:：-]?\s*/i;

const createStep = (title = '', description = ''): WorkflowStep => ({
  id: `step-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  title,
  description,
});

const parseWorkflowTextToSteps = (text: string): WorkflowStep[] => {
  const rawLines = text.split(/\r?\n/);
  if (rawLines.every(line => line.trim().length === 0)) {
    return [createStep()];
  }

  const parsed: Array<{ title: string; description: string }> = [];
  let current: { title: string; descriptionLines: string[] } | null = null;
  let inDescriptionBlock = false;

  const pushCurrent = () => {
    if (!current) return;

    const title = current.title.trim();
    const descriptionLines = [...current.descriptionLines];
    while (descriptionLines.length > 0 && descriptionLines[0].trim() === '') {
      descriptionLines.shift();
    }
    while (
      descriptionLines.length > 0 &&
      descriptionLines[descriptionLines.length - 1].trim() === ''
    ) {
      descriptionLines.pop();
    }
    const description = descriptionLines.join('\n').trimEnd();

    if (title || description) {
      parsed.push({ title, description });
    }
  };

  for (const rawLine of rawLines) {
    const line = rawLine.replace(/\s+$/, '');
    const trimmedLine = line.trim();

    if (trimmedLine.length === 0) {
      if (current && inDescriptionBlock) {
        current.descriptionLines.push('');
      }
      continue;
    }

    const isTopLevel = /^\S/.test(line);
    if (isTopLevel && STEP_PREFIX_REGEX.test(trimmedLine)) {
      pushCurrent();
      current = {
        title: trimmedLine.replace(STEP_PREFIX_REGEX, '').trim(),
        descriptionLines: [],
      };
      inDescriptionBlock = false;
      continue;
    }

    if (!current) {
      current = { title: trimmedLine, descriptionLines: [] };
      inDescriptionBlock = false;
      continue;
    }

    if (DESCRIPTION_PREFIX_REGEX.test(trimmedLine)) {
      const descriptionLine = trimmedLine
        .replace(DESCRIPTION_PREFIX_REGEX, '')
        .trimEnd();
      if (descriptionLine) {
        current.descriptionLines.push(descriptionLine);
      }
      inDescriptionBlock = true;
      continue;
    }

    if (!current.title) {
      current.title = trimmedLine;
      continue;
    }

    // Preserve manual line breaks and list formatting in description blocks.
    // Strip only one visual indentation level from serialized content.
    const normalizedLine = line.replace(/^\s{1,4}/, '');
    current.descriptionLines.push(normalizedLine);
    inDescriptionBlock = true;
  }

  pushCurrent();
  if (parsed.length === 0) {
    return [createStep()];
  }

  return parsed.map(step => createStep(step.title, step.description));
};

const buildWorkflowTextFromSteps = (
  steps: WorkflowStep[],
  labels: { stepLabel: string; descriptionLabel: string }
): string => {
  const { stepLabel, descriptionLabel } = labels;
  return steps
    .map(step => ({
      title: step.title.trim(),
      description: step.description,
    }))
    .filter(step => step.title || step.description)
    .map((step, index) => {
      const fallbackTitle = `${stepLabel} ${index + 1}`;
      const lines = [`${index + 1}. ${step.title.trim() || fallbackTitle}`];
      if (step.description) {
        const descriptionLines = step.description
          .split(/\r?\n/)
          .map(line => line.trimEnd())
          .filter(
            (line, lineIndex, arr) =>
              line.trim().length > 0 ||
              (lineIndex > 0 && lineIndex < arr.length - 1)
          );
        if (descriptionLines.length > 0) {
          lines.push(`   ${descriptionLabel}:`);
        }
        for (const descriptionLine of descriptionLines) {
          lines.push(descriptionLine ? `   ${descriptionLine}` : '');
        }
      }
      return lines.join('\n');
    })
    .join('\n\n');
};

export function WorkflowsComponent() {
  const t = useTranslation();
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [showDialog, setShowDialog] = useState(false);
  const [editingWorkflow, setEditingWorkflow] = useState<Workflow | null>(null);
  const [formData, setFormData] = useState({
    name: '',
    steps: [createStep()],
  });
  const [saving, setSaving] = useState(false);

  // Run-now dialog state
  const [showRunDialog, setShowRunDialog] = useState(false);
  const [runWorkflowTarget, setRunWorkflowTarget] = useState<Workflow | null>(
    null
  );
  const [devices, setDevices] = useState<Device[]>([]);
  const [runDeviceSerials, setRunDeviceSerials] = useState<string[]>([]);
  const [runExecutionMode, setRunExecutionMode] = useState<
    'classic' | 'layered'
  >('classic');
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  // Live execution state
  const [runPhase, setRunPhase] = useState<'form' | 'running'>('form');
  const [runTasks, setRunTasks] = useState<WorkflowRunTaskInfo[]>([]);
  const [taskEvents, setTaskEvents] = useState<
    Record<string, TaskEventRecordResponse[]>
  >({});
  const [taskStatuses, setTaskStatuses] = useState<Record<string, string>>({});
  const [stoppingTasks, setStoppingTasks] = useState<Record<string, boolean>>(
    {}
  );
  const streamsRef = useRef<Array<{ close: () => void }>>([]);

  const closeAllStreams = () => {
    streamsRef.current.forEach(s => s.close());
    streamsRef.current = [];
  };

  const resetRunState = () => {
    closeAllStreams();
    setRunPhase('form');
    setRunTasks([]);
    setTaskEvents({});
    setTaskStatuses({});
    setStoppingTasks({});
    setRunError(null);
    setRunning(false);
  };

  const loadWorkflows = async () => {
    try {
      setLoading(true);
      const data = await listWorkflows();
      setWorkflows(data.workflows);
    } catch (error) {
      console.error('Failed to load workflows:', error);
    } finally {
      setLoading(false);
    }
  };

  // Load workflows on mount
  useEffect(() => {
    queueMicrotask(() => {
      loadWorkflows();
    });
  }, []);

  const handleOpenRunDialog = async (workflow: Workflow) => {
    setRunWorkflowTarget(workflow);
    setRunDeviceSerials([]);
    setRunExecutionMode('classic');
    resetRunState();
    setShowRunDialog(true);
    try {
      const data = await getDevices();
      setDevices(data);
    } catch (error) {
      console.error('Failed to load devices:', error);
      setDevices([]);
    }
  };

  const isTaskActive = (status: string): boolean =>
    status === 'QUEUED' || status === 'RUNNING';

  const subscribeTaskStream = (taskId: string) => {
    const stream = streamTaskEvents(
      taskId,
      event => {
        setTaskEvents(prev => ({
          ...prev,
          [taskId]: [...(prev[taskId] || []), event],
        }));
        if (event.event_type === 'done') {
          const success = event.payload.success === true;
          setTaskStatuses(prev => ({
            ...prev,
            [taskId]: success ? 'SUCCEEDED' : 'FAILED',
          }));
        } else if (event.event_type === 'error') {
          setTaskStatuses(prev => ({ ...prev, [taskId]: 'FAILED' }));
        } else if (event.event_type === 'cancelled') {
          setTaskStatuses(prev => ({ ...prev, [taskId]: 'CANCELLED' }));
        } else {
          setTaskStatuses(prev =>
            prev[taskId] && !isTaskActive(prev[taskId])
              ? prev
              : { ...prev, [taskId]: 'RUNNING' }
          );
        }
      },
      () => {
        setTaskStatuses(prev =>
          prev[taskId] && !isTaskActive(prev[taskId])
            ? prev
            : { ...prev, [taskId]: 'FAILED' }
        );
      }
    );
    streamsRef.current.push(stream);
  };

  const handleStopTask = async (taskId: string) => {
    setStoppingTasks(prev => ({ ...prev, [taskId]: true }));
    try {
      await cancelTaskRun(taskId);
    } catch (error) {
      console.error('Failed to stop task:', error);
      // Roll back the stopping flag so the user can retry.
      setStoppingTasks(prev => ({ ...prev, [taskId]: false }));
    }
  };

  const handleStopAll = async () => {
    const activeIds = runTasks
      .map(task => task.task_id)
      .filter(id => isTaskActive(taskStatuses[id] || ''));
    await Promise.all(activeIds.map(id => handleStopTask(id)));
  };

  const hasActiveTask = runTasks.some(task =>
    isTaskActive(taskStatuses[task.task_id] || task.status)
  );

  const handleRunNow = async () => {
    if (!runWorkflowTarget || runDeviceSerials.length === 0) {
      setRunError(t.workflows.requireDevice);
      return;
    }
    try {
      setRunning(true);
      setRunError(null);
      const result = await runWorkflow(runWorkflowTarget.uuid, {
        device_serialnos: runDeviceSerials,
        execution_mode: runExecutionMode,
      });
      if (!result.success && result.tasks.length === 0) {
        setRunError(result.message || t.workflows.runFailed);
        setRunning(false);
        return;
      }
      // Enter live-view phase and subscribe to each device's event stream.
      setRunTasks(result.tasks);
      const initialStatuses: Record<string, string> = {};
      result.tasks.forEach(task => {
        initialStatuses[task.task_id] = task.status;
      });
      setTaskStatuses(initialStatuses);
      setRunPhase('running');
      result.tasks.forEach(task => {
        if (isTaskActive(task.status)) {
          subscribeTaskStream(task.task_id);
        }
      });
    } catch (error) {
      console.error('Failed to run workflow:', error);
      setRunError(t.workflows.runFailed);
    } finally {
      setRunning(false);
    }
  };

  const handleRunDialogOpenChange = (open: boolean) => {
    if (!open) {
      resetRunState();
    }
    setShowRunDialog(open);
  };

  const handleFillToChat = (workflow: Workflow) => {
    try {
      sessionStorage.setItem('workflow-prefill-text', workflow.text);
    } catch (error) {
      console.error('Failed to store prefill text:', error);
    }
    window.location.href = '/chat';
  };

  const handleCreate = () => {
    setEditingWorkflow(null);
    setFormData({ name: '', steps: [createStep()] });
    setShowDialog(true);
  };

  const handleEdit = (workflow: Workflow) => {
    setEditingWorkflow(workflow);
    setFormData({
      name: workflow.name,
      steps: parseWorkflowTextToSteps(workflow.text),
    });
    setShowDialog(true);
  };

  const updateStepTitle = (stepId: string, title: string) => {
    setFormData(prev => ({
      ...prev,
      steps: prev.steps.map(step =>
        step.id === stepId ? { ...step, title } : step
      ),
    }));
  };

  const updateStepDescription = (stepId: string, description: string) => {
    setFormData(prev => ({
      ...prev,
      steps: prev.steps.map(step =>
        step.id === stepId ? { ...step, description } : step
      ),
    }));
  };

  const insertStepAfter = (index: number) => {
    setFormData(prev => {
      const nextSteps = [...prev.steps];
      nextSteps.splice(index + 1, 0, createStep());
      return { ...prev, steps: nextSteps };
    });
  };

  const removeStep = (stepId: string) => {
    setFormData(prev => {
      if (prev.steps.length === 1) {
        return { ...prev, steps: [createStep()] };
      }
      return {
        ...prev,
        steps: prev.steps.filter(step => step.id !== stepId),
      };
    });
  };

  const moveStep = (index: number, direction: -1 | 1) => {
    setFormData(prev => {
      const targetIndex = index + direction;
      if (targetIndex < 0 || targetIndex >= prev.steps.length) {
        return prev;
      }
      const nextSteps = [...prev.steps];
      [nextSteps[index], nextSteps[targetIndex]] = [
        nextSteps[targetIndex],
        nextSteps[index],
      ];
      return { ...prev, steps: nextSteps };
    });
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const payload = {
        name: formData.name.trim(),
        text: buildWorkflowTextFromSteps(formData.steps, {
          stepLabel: t.workflows.stepLabel,
          descriptionLabel: t.workflows.stepDescriptionLabel,
        }),
      };
      if (editingWorkflow) {
        await updateWorkflow(editingWorkflow.uuid, payload);
      } else {
        await createWorkflow(payload);
      }
      setShowDialog(false);
      await loadWorkflows();
    } catch (error) {
      console.error('Failed to save workflow:', error);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (uuid: string) => {
    if (!window.confirm(t.workflows.deleteConfirm)) return;
    try {
      await deleteWorkflow(uuid);
      await loadWorkflows();
    } catch (error) {
      console.error('Failed to delete workflow:', error);
    }
  };

  const hasValidStep = formData.steps.some(step => step.title.trim());

  return (
    <div className="container mx-auto p-6 max-w-7xl">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold">{t.workflows.title}</h1>
        <Button onClick={handleCreate}>
          <Plus className="w-4 h-4 mr-2" />
          {t.workflows.createNew}
        </Button>
      </div>

      {loading ? (
        <div className="flex justify-center items-center h-64">
          <Loader2 className="w-8 h-8 animate-spin text-slate-400" />
        </div>
      ) : workflows.length === 0 ? (
        <div className="text-center py-12">
          <p className="text-slate-500 dark:text-slate-400">
            {t.workflows.empty}
          </p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {workflows.map(workflow => (
            <Card
              key={workflow.uuid}
              className="hover:shadow-md transition-shadow"
            >
              <CardHeader>
                <CardTitle className="text-lg">{workflow.name}</CardTitle>
              </CardHeader>
              <CardContent>
                {(() => {
                  const steps = parseWorkflowTextToSteps(workflow.text).filter(
                    step => step.title.trim() || step.description.trim()
                  );
                  const previewSteps = steps.slice(0, 3);
                  return (
                    <div className="mb-4 space-y-2">
                      <p className="text-xs text-slate-500 dark:text-slate-400">
                        {t.workflows.stepCount}: {steps.length}
                      </p>
                      {previewSteps.map((step, index) => (
                        <div key={`${workflow.uuid}-preview-${index}`}>
                          <p className="text-sm text-slate-600 dark:text-slate-400 line-clamp-1">
                            {index + 1}. {step.title || step.description}
                          </p>
                          {step.description.trim() && (
                            <p className="text-xs text-slate-500 dark:text-slate-400 line-clamp-1">
                              {t.workflows.stepDescriptionLabel}:{' '}
                              {step.description}
                            </p>
                          )}
                        </div>
                      ))}
                      {steps.length > 3 && (
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                          +{steps.length - 3} {t.workflows.moreSteps}
                        </p>
                      )}
                    </div>
                  );
                })()}
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => handleOpenRunDialog(workflow)}
                  >
                    <Play className="w-3 h-3 mr-1" />
                    {t.workflows.runNow}
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleFillToChat(workflow)}
                  >
                    <MessageSquare className="w-3 h-3 mr-1" />
                    {t.workflows.fillToChat}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleEdit(workflow)}
                  >
                    <Edit className="w-3 h-3 mr-1" />
                    {t.common.edit}
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => handleDelete(workflow.uuid)}
                  >
                    <Trash2 className="w-3 h-3 mr-1" />
                    {t.common.delete}
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Create/Edit Dialog: header/footer fixed, only step list scrolls */}
      <Dialog open={showDialog} onOpenChange={setShowDialog}>
        <DialogContent
          className="sm:max-w-[680px] max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden"
          onOpenAutoFocus={e => e.preventDefault()}
        >
          <DialogHeader className="flex-shrink-0 px-6 pt-6 pb-3 pr-12 border-b border-slate-200 dark:border-slate-800">
            <DialogTitle>
              {editingWorkflow ? t.workflows.edit : t.workflows.create}
            </DialogTitle>
          </DialogHeader>
          {/* Scrollable body: name + steps list only */}
          <div className="flex-1 min-h-0 overflow-y-auto px-6 py-4">
            <div className="space-y-4 pr-1">
              <div className="space-y-2">
                <Label htmlFor="name">{t.workflows.name}</Label>
                <Input
                  id="name"
                  value={formData.name}
                  onChange={e =>
                    setFormData(prev => ({ ...prev, name: e.target.value }))
                  }
                  placeholder={t.workflows.namePlaceholder}
                />
              </div>
              <div className="space-y-3">
                <Label>{t.workflows.steps}</Label>
                <div className="rounded-lg border bg-slate-50/40 dark:bg-slate-900/30">
                  <div className="space-y-3 p-3">
                    {formData.steps.map((step, index) => (
                      <div
                        key={step.id}
                        className="rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden bg-white dark:bg-slate-900 shadow-sm"
                      >
                        <div className="flex items-center justify-between px-3 py-2 bg-slate-100/90 dark:bg-slate-800/70 border-b border-slate-200 dark:border-slate-700">
                          <div className="flex items-center gap-2">
                            <span className="inline-flex items-center justify-center h-6 w-6 rounded-full bg-sky-500 text-white text-xs font-semibold">
                              {index + 1}
                            </span>
                            <p className="text-xs text-slate-700 dark:text-slate-200 font-semibold">
                              {t.workflows.stepLabel} {index + 1}
                            </p>
                          </div>
                          <div className="flex gap-1">
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 rounded-full"
                              disabled={index === 0}
                              onClick={() => moveStep(index, -1)}
                            >
                              <ArrowUp className="w-3 h-3" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 rounded-full"
                              disabled={index === formData.steps.length - 1}
                              onClick={() => moveStep(index, 1)}
                            >
                              <ArrowDown className="w-3 h-3" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 rounded-full text-sky-600 hover:text-sky-700"
                              onClick={() => insertStepAfter(index)}
                              title={t.workflows.addStep}
                            >
                              <Plus className="w-3 h-3" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 rounded-full text-red-600 hover:text-red-700"
                              onClick={() => removeStep(step.id)}
                            >
                              <Trash2 className="w-3 h-3" />
                            </Button>
                          </div>
                        </div>
                        <div className="p-3 space-y-3">
                          <div className="space-y-1">
                            <Label className="text-xs text-slate-600 dark:text-slate-300">
                              {t.workflows.stepName}
                            </Label>
                            <Input
                              value={step.title}
                              onChange={e =>
                                updateStepTitle(step.id, e.target.value)
                              }
                              placeholder={t.workflows.stepNamePlaceholder}
                              className="h-10 bg-white dark:bg-slate-950"
                            />
                          </div>
                          <div className="space-y-1">
                            <Label className="text-xs text-slate-600 dark:text-slate-300">
                              {t.workflows.stepDescription}
                            </Label>
                            <Textarea
                              value={step.description}
                              onChange={e =>
                                updateStepDescription(step.id, e.target.value)
                              }
                              placeholder={
                                t.workflows.stepDescriptionPlaceholder
                              }
                              rows={7}
                              className="resize-y min-h-[180px] !rounded-lg bg-white dark:bg-slate-950"
                            />
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  {!hasValidStep ? t.workflows.requireStep : '\u00A0'}
                </p>
              </div>
            </div>
          </div>
          <DialogFooter className="flex-shrink-0 border-t border-slate-200 dark:border-slate-800 px-6 py-4">
            <Button variant="outline" onClick={() => setShowDialog(false)}>
              {t.common.cancel}
            </Button>
            <Button
              onClick={handleSave}
              disabled={!formData.name.trim() || !hasValidStep || saving}
            >
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  {t.common.loading}
                </>
              ) : (
                t.common.save
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Run Now Dialog */}
      <Dialog open={showRunDialog} onOpenChange={handleRunDialogOpenChange}>
        <DialogContent className="sm:max-w-[640px] max-h-[85vh] flex flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle>
              {t.workflows.runDialogTitle}
              {runWorkflowTarget ? ` - ${runWorkflowTarget.name}` : ''}
            </DialogTitle>
          </DialogHeader>

          {runPhase === 'form' ? (
            <>
              <div className="space-y-4 py-2 overflow-y-auto">
                <div className="space-y-2">
                  <Label>{t.workflows.selectDevices}</Label>
                  {devices.length === 0 ? (
                    <p className="text-sm text-amber-600 dark:text-amber-400">
                      {t.workflows.noOnlineDevices}
                    </p>
                  ) : (
                    <div className="border rounded-md p-2 space-y-1 max-h-52 overflow-y-auto">
                      {devices.map(device => (
                        <label
                          key={device.serial}
                          className="flex items-center gap-2 p-2 hover:bg-slate-100 dark:hover:bg-slate-800 rounded cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={runDeviceSerials.includes(device.serial)}
                            onChange={e => {
                              const checked = e.target.checked;
                              setRunDeviceSerials(prev =>
                                checked
                                  ? [...prev, device.serial]
                                  : prev.filter(s => s !== device.serial)
                              );
                            }}
                            className="rounded border-gray-300"
                          />
                          <span className="text-sm">
                            {device.model || device.serial}
                          </span>
                          {device.state === 'online' && (
                            <span className="ml-auto w-2 h-2 bg-green-500 rounded-full" />
                          )}
                        </label>
                      ))}
                    </div>
                  )}
                </div>
                <div className="space-y-2">
                  <Label>{t.workflows.executionMode}</Label>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant={
                        runExecutionMode === 'classic' ? 'default' : 'outline'
                      }
                      size="sm"
                      onClick={() => setRunExecutionMode('classic')}
                    >
                      {t.workflows.classicMode}
                    </Button>
                    <Button
                      type="button"
                      variant={
                        runExecutionMode === 'layered' ? 'default' : 'outline'
                      }
                      size="sm"
                      onClick={() => setRunExecutionMode('layered')}
                    >
                      {t.workflows.layeredMode}
                    </Button>
                  </div>
                </div>
                {runError && (
                  <p className="text-sm text-red-600 dark:text-red-400">
                    {runError}
                  </p>
                )}
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => handleRunDialogOpenChange(false)}
                >
                  {t.common.cancel}
                </Button>
                <Button
                  onClick={handleRunNow}
                  disabled={runDeviceSerials.length === 0 || running}
                >
                  {running ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      {t.workflows.runningLabel}
                    </>
                  ) : (
                    <>
                      <Play className="w-4 h-4 mr-2" />
                      {t.workflows.runNow}
                    </>
                  )}
                </Button>
              </DialogFooter>
            </>
          ) : (
            <>
              <div className="flex-1 overflow-y-auto space-y-4 py-2 pr-1">
                {runTasks.map(task => (
                  <RunTaskProgress
                    key={task.task_id}
                    task={task}
                    events={taskEvents[task.task_id] || []}
                    status={taskStatuses[task.task_id] || task.status}
                    stopping={!!stoppingTasks[task.task_id]}
                    onStop={() => handleStopTask(task.task_id)}
                    labels={{
                      thinking: t.historyPage.thinkingLabel || 'Thinking',
                      action: t.historyPage.actionLabel || 'Action',
                      result: t.historyPage.resultLabel || 'Result',
                      running: t.workflows.runningLabel,
                      waiting: t.workflows.runningLabel,
                      stop: t.workflows.stop,
                      stopping: t.workflows.stopping,
                    }}
                  />
                ))}
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => resetRunState()}
                  disabled={hasActiveTask}
                >
                  <ChevronLeft className="w-4 h-4 mr-2" />
                  {t.workflows.backToForm}
                </Button>
                {hasActiveTask && (
                  <Button variant="destructive" onClick={handleStopAll}>
                    <Square className="w-4 h-4 mr-2" />
                    {t.workflows.stopAll}
                  </Button>
                )}
                <Button
                  onClick={() => handleRunDialogOpenChange(false)}
                >
                  {t.common.confirm}
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

interface RunTaskProgressProps {
  task: WorkflowRunTaskInfo;
  events: TaskEventRecordResponse[];
  status: string;
  stopping: boolean;
  onStop: () => void;
  labels: {
    thinking: string;
    action: string;
    result: string;
    running: string;
    waiting: string;
    stop: string;
    stopping: string;
  };
}

function RunTaskProgress({
  task,
  events,
  status,
  stopping,
  onStop,
  labels,
}: RunTaskProgressProps) {
  const isActive = status === 'QUEUED' || status === 'RUNNING';
  const isSuccess = status === 'SUCCEEDED';
  const isFailed = status === 'FAILED' || status === 'CANCELLED';

  // Build an ordered list of steps from the event stream. Thinking chunks are
  // streamed *before* the action executes, so we accumulate them into the
  // current (in-progress) step and finalize it when a "step" event arrives.
  // This lets the UI show reasoning as it happens instead of only after a step
  // completes.
  interface DisplayStep {
    step?: number;
    thinking: string;
    action?: Record<string, unknown>;
    done: boolean;
  }
  const displaySteps: DisplayStep[] = [];
  let current: DisplayStep = { thinking: '', done: false };
  let finalMessage = '';
  let hasFinal = false;

  for (const event of events) {
    if (event.event_type === 'thinking') {
      const chunk =
        typeof event.payload.chunk === 'string'
          ? event.payload.chunk
          : typeof event.payload.thinking === 'string'
            ? event.payload.thinking
            : '';
      current.thinking += chunk;
    } else if (event.event_type === 'step') {
      current.step = event.payload.step as number | undefined;
      const stepThinking = event.payload.thinking as string | undefined;
      // Prefer the accumulated stream; fall back to the step's own thinking.
      if (!current.thinking && stepThinking) {
        current.thinking = stepThinking;
      }
      current.action = event.payload.action as
        | Record<string, unknown>
        | undefined;
      current.done = true;
      displaySteps.push(current);
      current = { thinking: '', done: false };
    } else if (
      event.event_type === 'done' ||
      event.event_type === 'error' ||
      event.event_type === 'cancelled'
    ) {
      hasFinal = true;
      if (typeof event.payload.message === 'string') {
        finalMessage = event.payload.message;
      }
    }
  }
  // If there is an in-progress step with streamed thinking but no step event
  // yet, surface it as a live "thinking" block.
  if (current.thinking.trim().length > 0) {
    displaySteps.push(current);
  }

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 overflow-hidden">
      {/* Device header */}
      <div className="flex items-center justify-between px-3 py-2 bg-slate-100/80 dark:bg-slate-800/60 border-b border-slate-200 dark:border-slate-700">
        <div className="flex items-center gap-2">
          <Bot className="w-4 h-4 text-slate-500" />
          <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
            {task.device_serial}
          </span>
        </div>
        {isActive ? (
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-1 text-xs text-sky-600 dark:text-sky-400">
              <Loader2 className="w-3 h-3 animate-spin" />
              {labels.running}
            </span>
            <Button
              variant="destructive"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={onStop}
              disabled={stopping}
            >
              {stopping ? (
                <Loader2 className="w-3 h-3 mr-1 animate-spin" />
              ) : (
                <Square className="w-3 h-3 mr-1" />
              )}
              {stopping ? labels.stopping : labels.stop}
            </Button>
          </div>
        ) : isSuccess ? (
          <CheckCircle className="w-4 h-4 text-green-500" />
        ) : isFailed ? (
          <XCircle className="w-4 h-4 text-red-500" />
        ) : null}
      </div>

      {/* Steps */}
      <div className="p-3 space-y-3">
        {displaySteps.length === 0 && !hasFinal ? (
          <p className="flex items-center gap-1 text-xs text-slate-400 dark:text-slate-500">
            {isActive && <Loader2 className="w-3 h-3 animate-spin" />}
            {labels.waiting}
          </p>
        ) : (
          displaySteps.map((s, idx) => (
            <div
              key={`${task.task_id}-step-${idx}`}
              className="space-y-2 border-l-2 border-slate-200 dark:border-slate-700 pl-3"
            >
              <p className="flex items-center gap-1 text-xs font-semibold text-slate-500 dark:text-slate-400">
                {`#${s.step ?? idx + 1}`}
                {!s.done && <Loader2 className="w-3 h-3 animate-spin" />}
              </p>
              {s.thinking && (
                <div className="p-2 bg-slate-100 dark:bg-slate-800 rounded">
                  <p className="text-[11px] font-medium text-slate-500 dark:text-slate-400 mb-0.5">
                    {labels.thinking}
                  </p>
                  <p className="text-sm text-slate-700 dark:text-slate-300 whitespace-pre-wrap">
                    {s.thinking}
                  </p>
                </div>
              )}
              {s.action && (
                <div className="p-2 bg-amber-50 dark:bg-amber-900/20 rounded">
                  <p className="text-[11px] font-medium text-amber-600 dark:text-amber-400 mb-0.5">
                    {labels.action}
                  </p>
                  <pre className="text-xs text-slate-700 dark:text-slate-300 overflow-x-auto">
                    {JSON.stringify(s.action, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ))
        )}

        {hasFinal && finalMessage && (
          <div
            className={`p-2 rounded ${
              isSuccess
                ? 'bg-green-50 dark:bg-green-900/20'
                : 'bg-red-50 dark:bg-red-900/20'
            }`}
          >
            <p
              className={`text-[11px] font-medium mb-0.5 ${
                isSuccess
                  ? 'text-green-600 dark:text-green-400'
                  : 'text-red-600 dark:text-red-400'
              }`}
            >
              {labels.result}
            </p>
            <p className="text-sm text-slate-800 dark:text-slate-200 whitespace-pre-wrap">
              {finalMessage}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
