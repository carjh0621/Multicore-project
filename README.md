# Multicore Project

gem5, McPAT, HotSpot을 연결해 multicore out-of-order CPU의 **성능·전력·온도**를 평가하는 시뮬레이션 flow를 구축하는 프로젝트입니다.

현재는 세 도구를 Git submodule로 등록한 초기 단계이며, 도구 간 데이터 변환과 통합 실행 스크립트는 아직 구현되지 않았습니다.

## 평가 흐름

```text
Workload + CPU/cache configuration
                ↓
              gem5 → 성능 및 활동 통계
                ↓ 통계를 McPAT 입력 XML로 변환
              McPAT → 전력 및 면적 추정
                ↓ 블록별 전력 + floorplan + 열 모델 설정
              HotSpot → 온도 추정
```

## 처음 내려받기

Git이 설치된 환경에서 submodule을 포함해 clone합니다.

```bash
git clone --recurse-submodules https://github.com/carjh0621/Multicore-project.git
cd Multicore-project
```

이미 일반 `git clone`으로 내려받았다면 프로젝트 디렉터리에서 다음을 실행합니다.

```bash
git submodule update --init --recursive
```

위 명령은 소스를 내려받습니다. 각 도구의 빌드와 실행은 별도로 필요하며, 현재는 각 저장소의 안내를 따릅니다.

## 디렉터리 구조

```text
external/
├── gem5/       # CPU 및 메모리 시스템 시뮬레이터
├── mcpat/      # 전력 및 면적 모델
└── hotspot/    # 열 시뮬레이터
```

다른 환경에서도 같은 버전을 사용할 수 있도록 상위 저장소에서 각 submodule의 커밋을 고정합니다.

## 외부 소프트웨어 및 라이선스

- [gem5](https://github.com/gem5/gem5)
- [McPAT](https://github.com/HewlettPackard/mcpat)
- [HotSpot](https://github.com/uvahotspot/HotSpot)

각 도구에는 원본 저장소의 라이선스가 적용됩니다. `external/` 내부의 라이선스 파일, 저작권 고지 및 소스 헤더를 유지하며, 개별 파일과 포함된 라이브러리에는 별도 라이선스가 적용될 수 있습니다.
