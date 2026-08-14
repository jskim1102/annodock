import { type FormEvent, useEffect, useState } from "react";

import {
  getDataset,
  getProjectClassImageCounts,
  type DatasetDetail,
  type ProjectClassImageCount,
} from "../api/client";
import {
  getModels,
  getTrainingRecommendation,
  startTraining,
  type ModelPreset,
  type TrainingRecommendation,
  type TrainingOptimizer,
} from "../api/training";
import { AppShell, BreadcrumbLink } from "../components/AppShell";
import { Icon } from "../components/Icon";
import { SelectMenu } from "../components/SelectMenu";
import { appHref, navigate } from "../navigation";
import { getRecommendedRatios } from "../utils/trainingRatios";
import { RTX_3090_TRAINING_DEFAULTS } from "../utils/trainingArguments";

type SplitKey = "train" | "valid" | "test";

export function TrainPage({ datasetId }: { datasetId: number }) {
  const [dataset, setDataset] = useState<DatasetDetail | null>(null);
  const [classImageCounts, setClassImageCounts] = useState<ProjectClassImageCount[]>([]);
  const [classImageCountsLoading, setClassImageCountsLoading] = useState(true);
  const [classImageCountsError, setClassImageCountsError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [weights, setWeights] = useState("");
  const [splitMode, setSplitMode] = useState<"2way" | "3way">("3way");
  const [ratios, setRatios] = useState({ train: 70, valid: 20, test: 10 });
  const [excludeUnlabeledImages, setExcludeUnlabeledImages] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.exclude_unlabeled_images);
  const [includeUnlabeledImagesInTest, setIncludeUnlabeledImagesInTest] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.include_unlabeled_images_in_test);
  const [epochs, setEpochs] = useState(String(RTX_3090_TRAINING_DEFAULTS.epochs));
  const [imgsz, setImgsz] = useState(String(RTX_3090_TRAINING_DEFAULTS.imgsz));
  const [batch, setBatch] = useState(String(RTX_3090_TRAINING_DEFAULTS.batch));
  const [optimizer, setOptimizer] = useState<TrainingOptimizer>(RTX_3090_TRAINING_DEFAULTS.optimizer);
  const [lr0, setLr0] = useState(String(RTX_3090_TRAINING_DEFAULTS.lr0));
  const [lrf, setLrf] = useState(String(RTX_3090_TRAINING_DEFAULTS.lrf));
  const [warmupEpochs, setWarmupEpochs] = useState(String(RTX_3090_TRAINING_DEFAULTS.warmup_epochs));
  const [cosLr, setCosLr] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.cos_lr);
  const [patience, setPatience] = useState(String(RTX_3090_TRAINING_DEFAULTS.patience));
  const [augment, setAugment] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.augment);
  const [mosaic, setMosaic] = useState(String(RTX_3090_TRAINING_DEFAULTS.mosaic));
  const [mixup, setMixup] = useState(String(RTX_3090_TRAINING_DEFAULTS.mixup));
  const [hsvH, setHsvH] = useState(String(RTX_3090_TRAINING_DEFAULTS.hsv_h));
  const [hsvS, setHsvS] = useState(String(RTX_3090_TRAINING_DEFAULTS.hsv_s));
  const [hsvV, setHsvV] = useState(String(RTX_3090_TRAINING_DEFAULTS.hsv_v));
  const [fliplr, setFliplr] = useState(String(RTX_3090_TRAINING_DEFAULTS.fliplr));
  const [scale, setScale] = useState(String(RTX_3090_TRAINING_DEFAULTS.scale));
  const [translate, setTranslate] = useState(String(RTX_3090_TRAINING_DEFAULTS.translate));
  const [workers, setWorkers] = useState(String(RTX_3090_TRAINING_DEFAULTS.workers));
  const [cache, setCache] = useState<"none" | "ram" | "disk">(RTX_3090_TRAINING_DEFAULTS.cache);
  const [amp, setAmp] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.amp);
  const [compile, setCompile] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.compile);
  const [deterministic, setDeterministic] = useState<boolean>(RTX_3090_TRAINING_DEFAULTS.deterministic);
  const [savePeriod, setSavePeriod] = useState(String(RTX_3090_TRAINING_DEFAULTS.save_period));
  const [seed, setSeed] = useState("");
  const [multiScale, setMultiScale] = useState(String(RTX_3090_TRAINING_DEFAULTS.multi_scale));
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [runId, setRunId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [recommendation, setRecommendation] = useState<TrainingRecommendation | null>(null);
  const [recommendationLoading, setRecommendationLoading] = useState(false);
  const [recommendationError, setRecommendationError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setClassImageCounts([]);
    setClassImageCountsLoading(true);
    setClassImageCountsError(null);
    void Promise.all([getDataset(datasetId), getModels()])
      .then(([detail, presets]) => {
        if (!active) return;
        setDataset(detail);
        setModels(presets);
        setWeights(presets.find((model) => model.name === "yolo26s.pt")?.name ?? presets[0]?.name ?? "");
        void getProjectClassImageCounts(detail.project_id, [datasetId])
          .then((result) => {
            if (active) setClassImageCounts(result.items);
          })
          .catch((reason: unknown) => {
            if (active) {
              setClassImageCountsError(reason instanceof Error
                ? reason.message
                : "클래스별 이미지 수를 불러오지 못했습니다.");
            }
          })
          .finally(() => {
            if (active) setClassImageCountsLoading(false);
          });
      })
      .catch((reason: unknown) => {
        if (active) {
          setClassImageCountsLoading(false);
          setError(reason instanceof Error ? reason.message : "학습 설정을 불러오지 못했습니다.");
        }
      });
    return () => { active = false; };
  }, [datasetId]);

  const applyRecommendation = (next: TrainingRecommendation) => {
    setRecommendation(next);
    setEpochs(String(next.epochs));
    setImgsz(String(next.imgsz));
    setBatch(String(next.batch));
    setOptimizer(next.optimizer);
    setLr0(String(next.lr0));
    setWarmupEpochs(String(next.warmup_epochs));
    setPatience(String(next.patience));
    setMosaic(String(next.mosaic));
    setMixup(String(next.mixup));
    setScale(String(next.scale));
    setAmp(next.amp);
    setCompile(next.compile);
  };

  const loadRecommendation = async (options?: { useCurrentValues?: boolean }) => {
    if (!dataset || !weights) return;
    setRecommendationLoading(true);
    setRecommendationError(null);
    try {
      const next = await getTrainingRecommendation(dataset.id, {
        weights,
        imgsz: options?.useCurrentValues ? Number(imgsz) : RTX_3090_TRAINING_DEFAULTS.imgsz,
        multiScale: options?.useCurrentValues ? Number(multiScale) : RTX_3090_TRAINING_DEFAULTS.multi_scale,
        trainRatio: ratios.train / 100,
        excludeUnlabeledImages,
        includeUnlabeledImagesInTest,
      });
      applyRecommendation(next);
    } catch (reason: unknown) {
      setRecommendationError(
        reason instanceof Error ? reason.message : "추천값을 계산하지 못했습니다.",
      );
    } finally {
      setRecommendationLoading(false);
    }
  };

  useEffect(() => {
    if (!dataset || !weights) return;
    let active = true;
    setRecommendationLoading(true);
    setRecommendationError(null);
    void getTrainingRecommendation(dataset.id, {
      weights,
      imgsz: RTX_3090_TRAINING_DEFAULTS.imgsz,
      multiScale: RTX_3090_TRAINING_DEFAULTS.multi_scale,
      trainRatio: ratios.train / 100,
      excludeUnlabeledImages,
      includeUnlabeledImagesInTest,
    })
      .then((next) => {
        if (active) applyRecommendation(next);
      })
      .catch((reason: unknown) => {
        if (active) {
          setRecommendationError(
            reason instanceof Error ? reason.message : "추천값을 계산하지 못했습니다.",
          );
        }
      })
      .finally(() => {
        if (active) setRecommendationLoading(false);
      });
    return () => { active = false; };
  }, [dataset, weights, excludeUnlabeledImages, includeUnlabeledImagesInTest]);

  const totalImages = dataset?.image_count ?? 0;
  const eligibleImages = (
    excludeUnlabeledImages
      ? recommendation?.labeled_images ?? totalImages
      : totalImages
  );
  const unlabeledTestImages = includeUnlabeledImagesInTest
    ? recommendation?.unlabeled_images ?? 0
    : 0;
  const selectedTotal = excludeUnlabeledImages && !includeUnlabeledImagesInTest
    ? eligibleImages
    : totalImages;
  const ratioTotal = ratios.train + ratios.valid + (splitMode === "3way" ? ratios.test : 0);
  const splitKeys: SplitKey[] = splitMode === "3way" ? ["train", "valid", "test"] : ["train", "valid"];
  const recommendedRatios = getRecommendedRatios(ratios.train, ratios.valid, splitMode);
  const recommendedRatioText = recommendedRatios === null
    ? "train과 valid의 합을 100 이하로 입력하세요"
    : splitMode === "2way"
      ? `train ${recommendedRatios.train} · valid ${recommendedRatios.valid}`
      : `train ${recommendedRatios.train} · valid ${recommendedRatios.valid} · test ${recommendedRatios.test}`;
  const ratioGap = 100 - ratioTotal;
  const ratioStatusText = ratioGap === 0
    ? "현재 합 100"
    : ratioGap > 0
      ? `현재 합 ${ratioTotal} · ${ratioGap} 더 필요`
      : `현재 합 ${ratioTotal} · ${Math.abs(ratioGap)} 줄이기`;
  const counts = {
    train: Math.round(eligibleImages * ratios.train / 100),
    valid: Math.round(eligibleImages * ratios.valid / 100),
    test: splitMode === "3way"
      ? eligibleImages - Math.round(eligibleImages * ratios.train / 100) - Math.round(eligibleImages * ratios.valid / 100) + unlabeledTestImages
      : 0,
  };

  const setMode = (mode: "2way" | "3way") => {
    setSplitMode(mode);
    if (mode === "2way") setIncludeUnlabeledImagesInTest(false);
    setRatios(mode === "2way"
      ? { train: 80, valid: 20, test: 0 }
      : { train: 70, valid: 20, test: 10 });
    setError(null);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (ratioTotal !== 100) {
      setError(`분할 비율의 합은 100이어야 합니다. 현재 ${ratioTotal}입니다.`);
      return;
    }
    if (!weights) {
      setError("사용할 preset을 선택하세요.");
      return;
    }
    if (compile && Number(multiScale) > 0) {
      setError("Compile과 Multi-scale은 동시에 사용할 수 없습니다.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setWarnings([]);
    setRunId(null);
    try {
      const parsedSeed = seed.trim() === "" ? undefined : Number(seed);
      const result = await startTraining(datasetId, {
        weights,
        epochs: Number(epochs),
        imgsz: Number(imgsz),
        batch: Number(batch),
        device: 0,
        optimizer,
        lr0: Number(lr0),
        lrf: Number(lrf),
        warmup_epochs: Number(warmupEpochs),
        cos_lr: cosLr,
        patience: Number(patience),
        augment,
        mosaic: Number(mosaic),
        mixup: Number(mixup),
        copy_paste: 0,
        close_mosaic: RTX_3090_TRAINING_DEFAULTS.close_mosaic,
        hsv_h: Number(hsvH),
        hsv_s: Number(hsvS),
        hsv_v: Number(hsvV),
        fliplr: Number(fliplr),
        scale: Number(scale),
        translate: Number(translate),
        workers: Number(workers),
        cache,
        amp,
        compile,
        deterministic,
        save_period: Number(savePeriod),
        multi_scale: Number(multiScale),
        exclude_unlabeled_images: excludeUnlabeledImages,
        include_unlabeled_images_in_test: includeUnlabeledImagesInTest,
        split_mode: splitMode,
        ratios: Object.fromEntries(splitKeys.map((key) => [key, ratios[key] / 100])),
        ...(parsedSeed === undefined ? {} : { seed: parsedSeed }),
      });
      setWarnings(result.warnings);
      setRunId(result.run_id);
      if (result.warnings.length === 0) navigate(`/runs/${result.run_id}`);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "학습을 시작하지 못했습니다.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppShell
      active="projects"
      breadcrumb={<><BreadcrumbLink href="/projects">프로젝트</BreadcrumbLink><span>/</span><BreadcrumbLink href="/projects">{dataset?.name ?? `데이터셋 ${datasetId}`}</BreadcrumbLink><span>/</span><strong>학습</strong></>}
    >
      <h1 className="page-title">학습 설정</h1>
      <form onSubmit={(event) => void submit(event)}>
        <div className="training-layout">
          <section className="card training-form-card">
            <div className="field">
              <label className="training-section-label" htmlFor="run-name">run</label>
              <input className="input" id="run-name" value={dataset ? `${dataset.name} · 자동 생성` : "자동 생성"} disabled readOnly />
            </div>
            <div className="field">
              <span className="field-label training-section-label">분할</span>
              <div className="seg">
                <button className="seg-opt" type="button" aria-pressed={splitMode === "2way"} onClick={() => setMode("2way")}>2-way · train/valid</button>
                <button className="seg-opt" type="button" aria-pressed={splitMode === "3way"} onClick={() => setMode("3way")}>3-way · train/valid/test</button>
              </div>
              <div className={`ratio-grid ratio-${splitMode}`}>
                {splitKeys.map((key) => <label key={key}><span className="sr-only">{key} %</span><input className={`input${ratioTotal !== 100 ? " is-error" : ""}`} data-numeric type="number" min="0" max="100" value={ratios[key]} aria-label={`${key} %`} onChange={(event) => setRatios((current) => ({ ...current, [key]: Number(event.target.value) }))} /></label>)}
              </div>
              <div className={`split-ratio-guidance${ratioGap === 0 ? "" : " is-warning"}`}>
                <span>권장값 · {recommendedRatioText}</span>
                <span>{ratioStatusText}</span>
              </div>
              <div className="training-option-list training-data-options">
                <label
                  className={`training-data-option${excludeUnlabeledImages ? " is-selected" : ""}`}
                  htmlFor="exclude-unlabeled-images"
                >
                  <input
                    className="training-data-checkbox"
                    id="exclude-unlabeled-images"
                    type="checkbox"
                    checked={excludeUnlabeledImages}
                    onChange={(event) => {
                      const checked = event.target.checked;
                      setExcludeUnlabeledImages(checked);
                      if (!checked) setIncludeUnlabeledImagesInTest(false);
                    }}
                  />
                  <span className="training-data-option-copy">
                    <span className="training-data-option-title">
                      <b>라벨 없는 이미지 제외</b>
                    </span>
                    <small>
                      라벨 소스와 bbox가 없는 이미지를 제외하고 나머지만 분할합니다.
                    </small>
                  </span>
                </label>
                <label
                  className={`training-data-option${includeUnlabeledImagesInTest ? " is-selected" : ""}${splitMode !== "3way" || !excludeUnlabeledImages ? " is-disabled" : ""}`}
                  htmlFor="include-unlabeled-images-in-test"
                >
                  <input
                    className="training-data-checkbox"
                    id="include-unlabeled-images-in-test"
                    type="checkbox"
                    checked={includeUnlabeledImagesInTest}
                    disabled={splitMode !== "3way" || !excludeUnlabeledImages}
                    onChange={(event) => setIncludeUnlabeledImagesInTest(event.target.checked)}
                  />
                  <span className="training-data-option-copy">
                    <span className="training-data-option-title">
                      <b>라벨 없는 이미지 test에 포함</b>
                      <span className="training-data-option-badge">3-way 전용</span>
                    </span>
                    <small>
                      제외된 라벨 없는 이미지를 test에만 추가합니다.
                    </small>
                  </span>
                </label>
              </div>
            </div>
            <div className="field">
              <label className="training-section-label" htmlFor="preset">Model</label>
              <SelectMenu
                id="preset"
                value={weights}
                options={models.map((model) => ({
                  value: model.name,
                  label: `${model.name.replace(".pt", "")}${model.size_mb === null ? "" : ` · ${model.size_mb.toFixed(1)} MB`}`,
                }))}
                onChange={setWeights}
              />
            </div>
            <div className="recommendation-title-row">
              <h2 className="training-section-label training-parameter-title">기본 파라미터</h2>
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={!dataset || !weights || recommendationLoading}
                onClick={() => void loadRecommendation({ useCurrentValues: true })}
              >
                {recommendationLoading ? "계산 중…" : "RTX 3090 추천값 적용"}
              </button>
            </div>
            {recommendation ? (
              <div className="training-recommendation" role="status">
                <strong>추천 적용됨</strong>
                <span>
                  {excludeUnlabeledImages
                    ? `라벨 없는 이미지 ${recommendation.unlabeled_images.toLocaleString()}장 제외 · `
                    : includeUnlabeledImagesInTest
                      ? `라벨 없는 이미지 ${recommendation.unlabeled_images.toLocaleString()}장 test 전용 · `
                      : ""}train {recommendation.train_images.toLocaleString()}장 · 작은 객체 {(recommendation.small_object_ratio * 100).toFixed(1)}% · 최대 해상도 {recommendation.effective_max_imgsz} · batch {recommendation.batch}
                </span>
              </div>
            ) : null}
            {recommendationError ? <p className="training-recommendation-error" role="alert">{recommendationError}</p> : null}
            <div className="two-field-grid">
              <div className="field"><label htmlFor="epochs">epochs</label><input className="input" id="epochs" data-numeric required type="number" min="1" value={epochs} onChange={(event) => setEpochs(event.target.value)} /></div>
              <div className="field"><label htmlFor="imgsz">imgsz</label><input className="input" id="imgsz" data-numeric required type="number" min="1" value={imgsz} onChange={(event) => setImgsz(event.target.value)} /></div>
            </div>
            <div className="two-field-grid">
              <div className="field"><label htmlFor="batch">batch</label><input className="input" id="batch" data-numeric required type="number" min="-1" value={batch} onChange={(event) => setBatch(event.target.value)} /><div className="hint">모델·최대 해상도 기준 RTX 3090 권장값</div></div>
            </div>
            <div className="training-parameter-sections">
              <section className="training-parameter-section">
                <h2 className="training-section-label training-parameter-title">최적화 파라미터</h2>
                <div className="training-parameter-panel">
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="optimizer">optimizer</label><SelectMenu id="optimizer" value={optimizer} options={["AdamW", "Adam", "Adamax", "NAdam", "RAdam", "RMSProp", "SGD", "MuSGD", "auto"].map((item) => ({ value: item, label: item }))} onChange={(nextValue) => setOptimizer(nextValue as TrainingOptimizer)} /></div>
                    <div className="field"><label htmlFor="lr0">lr0</label><input className="input" id="lr0" data-numeric required type="number" min="0.000001" max="1" step="any" value={lr0} onChange={(event) => setLr0(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="lrf">lrf</label><input className="input" id="lrf" data-numeric required type="number" min="0" max="1" step="any" value={lrf} onChange={(event) => setLrf(event.target.value)} /></div>
                    <div className="field"><label htmlFor="warmup-epochs">warmup epochs</label><input className="input" id="warmup-epochs" data-numeric required type="number" min="0" step="any" value={warmupEpochs} onChange={(event) => setWarmupEpochs(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="patience">patience</label><input className="input" id="patience" data-numeric required type="number" min="0" value={patience} onChange={(event) => setPatience(event.target.value)} /></div>
                  </div>
                  <div className="training-option-list">
                    <label><input type="checkbox" checked={cosLr} onChange={(event) => setCosLr(event.target.checked)} /><span><b>Cosine LR</b><small>후반 학습률을 부드럽게 낮춤</small></span></label>
                  </div>
                </div>
              </section>
              <section className="training-parameter-section">
                <h2 className="training-section-label training-parameter-title">성능 파라미터</h2>
                <div className="training-parameter-panel">
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="device">device</label><input className="input" id="device" data-numeric type="number" value="0" disabled readOnly /><div className="hint">RTX 3090 단일 GPU</div></div>
                    <div className="field"><label htmlFor="workers">workers</label><input className="input" id="workers" data-numeric required type="number" min="0" max="128" value={workers} onChange={(event) => setWorkers(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="cache">cache</label><SelectMenu id="cache" value={cache} options={[{ value: "none", label: "사용 안 함" }, { value: "ram", label: "RAM" }, { value: "disk", label: "Disk" }]} onChange={(nextValue) => setCache(nextValue as "none" | "ram" | "disk")} /><div className="hint">시스템 메모리 여유 시 RAM 권장</div></div>
                  </div>
                  <div className="training-option-list">
                    <label><input type="checkbox" checked={amp} onChange={(event) => setAmp(event.target.checked)} /><span><b>AMP</b><small>켜짐 권장</small></span></label>
                    <label><input type="checkbox" checked={compile} onChange={(event) => setCompile(event.target.checked)} /><span><b>Compile</b><small>Multi-scale과 동시 사용 불가</small></span></label>
                    <label><input type="checkbox" checked={deterministic} onChange={(event) => setDeterministic(event.target.checked)} /><span><b>재현 가능한 학습</b><small>끄면 3090 처리량 우선</small></span></label>
                  </div>
                </div>
              </section>
              <section className="training-parameter-section">
                <h2 className="training-section-label training-parameter-title">증강 파라미터</h2>
                <div className="training-parameter-panel">
                  <div className="training-option-list">
                    <label><input type="checkbox" checked={augment} onChange={(event) => setAugment(event.target.checked)} /><span><b>Augment</b><small>학습 데이터 증강 사용</small></span></label>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="mosaic">mosaic</label><input className="input" id="mosaic" data-numeric required type="number" min="0" max="1" step="any" value={mosaic} onChange={(event) => setMosaic(event.target.value)} /></div>
                    <div className="field"><label htmlFor="mixup">mixup</label><input className="input" id="mixup" data-numeric required type="number" min="0" max="1" step="any" value={mixup} onChange={(event) => setMixup(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="hsv-h">hsv h</label><input className="input" id="hsv-h" data-numeric required type="number" min="0" max="1" step="any" value={hsvH} onChange={(event) => setHsvH(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="hsv-s">hsv s</label><input className="input" id="hsv-s" data-numeric required type="number" min="0" max="1" step="any" value={hsvS} onChange={(event) => setHsvS(event.target.value)} /></div>
                    <div className="field"><label htmlFor="hsv-v">hsv v</label><input className="input" id="hsv-v" data-numeric required type="number" min="0" max="1" step="any" value={hsvV} onChange={(event) => setHsvV(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="fliplr">fliplr</label><input className="input" id="fliplr" data-numeric required type="number" min="0" max="1" step="any" value={fliplr} onChange={(event) => setFliplr(event.target.value)} /></div>
                    <div className="field"><label htmlFor="scale">scale</label><input className="input" id="scale" data-numeric required type="number" min="0" max="1" step="any" value={scale} onChange={(event) => setScale(event.target.value)} /></div>
                  </div>
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="translate">translate</label><input className="input" id="translate" data-numeric required type="number" min="0" max="1" step="any" value={translate} onChange={(event) => setTranslate(event.target.value)} /></div>
                    <div className="field"><label htmlFor="multi-scale">multi scale</label><input className="input" id="multi-scale" data-numeric required type="number" min="0" max="1" step="any" value={multiScale} onChange={(event) => { const value = event.target.value; setMultiScale(value); if (Number(value) > 0) setCompile(false); }} /><div className="hint">0 = 꺼짐 · 켜면 최대 해상도로 batch 계산</div></div>
                  </div>
                </div>
              </section>
              <section className="training-parameter-section">
                <h2 className="training-section-label training-parameter-title">운영 파라미터</h2>
                <div className="training-parameter-panel">
                  <div className="two-field-grid">
                    <div className="field"><label htmlFor="save-period">save period</label><input className="input" id="save-period" data-numeric required type="number" min="-1" value={savePeriod} onChange={(event) => setSavePeriod(event.target.value)} /><div className="hint">25 epoch마다 중간 저장</div></div>
                    <div className="field"><label htmlFor="seed">seed</label><input className="input" id="seed" data-numeric type="number" value={seed} placeholder="비우면 서버 생성" onChange={(event) => setSeed(event.target.value)} /><div className="hint">비우면 서버가 자동 생성</div></div>
                  </div>
                </div>
              </section>
            </div>
          </section>

          <section className="card training-summary-card">
            <h2 className="card-title">{dataset?.name ?? "데이터셋 로딩 중"}</h2>
            <div className="card-meta">학습 데이터 — 단일 데이터셋</div>
            <div className="training-dataset-list"><div className="training-dataset-row"><span className="checkbox is-on"><Icon name="check" size={10} /></span><span>{dataset?.name ?? `데이터셋 ${datasetId}`}</span><span className="mono">{selectedTotal.toLocaleString()}</span></div></div>
            <div className="training-class-image-list" aria-label="학습 데이터셋의 클래스 이미지 수 및 전체 이미지 수">
              {classImageCountsError ? (
                <span className="training-class-image-message is-error">{classImageCountsError}</span>
              ) : classImageCountsLoading ? (
                <span className="training-class-image-message">집계 중…</span>
              ) : classImageCounts.length > 0 ? (
                classImageCounts.map((item) => (
                  <span className="training-class-stat" key={item.class_id}>
                    <span className="training-class-stat-heading">
                      <i aria-hidden="true" style={{ background: item.color }} />
                      <span>{item.name}</span>
                    </span>
                    <strong className="training-class-stat-value mono">{item.image_count.toLocaleString()}장</strong>
                  </span>
                ))
              ) : (
                <span className="training-class-image-message">등록된 클래스가 없습니다.</span>
              )}
              <span className="training-class-stat is-total">
                <span className="training-class-stat-heading">전체 이미지</span>
                <strong className="training-class-stat-value mono">{selectedTotal.toLocaleString()}장</strong>
              </span>
            </div>
            <hr className="hr" />
            <div className="summary-kicker">분할 미리보기</div>
            <div className="split-preview">{splitKeys.map((key) => <div key={key}><span>{key}</span><span className="bar"><i style={{ width: `${ratios[key]}%` }} /></span><span className="mono">{counts[key].toLocaleString()}</span></div>)}</div>
          </section>
        </div>

        <div className="training-actions">
          {warnings.length > 0 && runId !== null ? <div className="banner-danger">{warnings.map((warning) => <div key={warning}>{warning}</div>)}<a href={appHref(`/runs/${runId}`)}>run 보기 →</a></div> : null}
          <button className="btn btn-primary" type="submit" disabled={submitting || !dataset || models.length === 0}><Icon name="cpu" size={14} />{submitting ? "제출 중…" : "학습 시작"}</button>
        </div>
        {error ? <p className="training-error" role="alert">{error}{error.includes("학습 중") ? <> · <a href={appHref("/runs")}>현재 run 보기</a></> : null}</p> : null}
      </form>
    </AppShell>
  );
}
