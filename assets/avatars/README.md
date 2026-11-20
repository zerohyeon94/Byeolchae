# 직원 프로필 사진

Discord 웹훅 게시에 붙이는 직원 사진이다 ([ADR-015](../../docs/아키텍처%20결정/ADR-015-직원-프로필-사진.md)). 파일 이름은 별 이름의 영문 소문자다(ADR-012). `byeolchae.png`는 `[별채]` 게시용이고, 봇 계정 아이콘과 같은 그림이다(원화 `00-별채/별채.png`).

- 별직원 원화(`output/characters/원화/`, 2026-11-09)에서 캐릭터를 가운데에 두고 원화 배경색으로 채운 512x512 PNG다
- Discord가 사진을 원으로 자르므로 캐릭터 둘레에 여백을 둔다
- 원화를 고치면 `python scripts/make_avatars.py`로 다시 만든다 (Pillow 필요)

## 적용

비공개 회사 설정 `~/.zerosync/company/org.yaml`에 주소를 적는다. 주소는 develop에 merge된 뒤부터 열린다.

```yaml
bot_avatar: https://raw.githubusercontent.com/zerohyeon94/Byeolchae/develop/assets/avatars/byeolchae.png

agents:
  polaris:
    display: 폴라리스
    avatar: https://raw.githubusercontent.com/zerohyeon94/Byeolchae/develop/assets/avatars/polaris.png
    # ...
```

| 묶음 | 팀 | 파일 |
|---|---|---|
| 회사 | 별채 | byeolchae |
| 대표 직속 | 비서 | polaris, dubhe |
| | 교육 | thuban, mira, capella |
| | 리서치 | canopus, procyon, deneb |
| 경영지원 | 경영 | regulus, merak |
| | 인사 | castor, vega |
| | 재무 | spica, fomalhaut |
| 제품 | 기획 | arcturus, aldebaran, sirius, pollux |
| | 디자인 | albireo, altair, betelgeuse |
| | 개발 | rigel, alnitak, mintaka, alnilam, bellatrix |
| | QA | mizar, alcor, shaula |
| 시장 | 마케팅 | peacock, acrux, hadar |
| | 영업 | alphecca, elnath |
| | CS | achernar, ankaa |
| 콘텐츠 | 채널 관리 | antares, denebola |
| | 영상 제작 | sadr, kochab, alkaid |
