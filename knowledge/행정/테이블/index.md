# 행정 테이블 정리

> `D:\DeskList\업무\행정`(행정시스템 소스·인수인계 쿼리 7,700여 파일)에서 사용되는 행정 시스템(MS-SQL) DB 테이블을 도메인별로 정리.
> 코드 사용 빈도는 원본 폴더의 `D:\DeskList\업무\행정\공통코드_사용_리스트.md` 참고. 컬럼–공통코드 매핑은 [knowledge/행정](../index.md)·[knowledge/공통](../../공통/index.md)과 상호 링크.

## 도메인 문서
- [인사](인사.md) — `StaffMaster`(교직원 마스터)·`AppmntInfo`(발령)·임용·경력·증명·출장·근태·연수·평가
- [급여](급여.md) — `AGY_` 급여명세·호봉표·수당·공제·4대보험·연말정산(YETA)
- [자산·구매](자산·구매.md) — `GoodsMaster`·`InstrumentAcq`·`Depreciation`·`EquipPurc*`
- [결재·회계](결재·회계.md) — `ResolutionMaster`(지출결의서)·`AcctCode`·`BussCode`
- [기부·통계·강사료](기부·통계·강사료.md) — `SGB_`(기부)·`KEDI_`(통계)·`SSE_`(강사료)
- [조직·공통](조직·공통.md) — `Organization`·`COR_`·`BaseCode`

## 테이블 ↔ 도메인
| 접두어/테이블 | 도메인 | 핵심 테이블 |
|---|---|---|
| StaffMaster, Appmnt* | 인사 | StaffMaster, AppmntInfo, AppmntReport |
| AGY_ | 급여 | AGY_PayMonthDesc, AGY_PaySalaryStep, AGY_Dedu* |
| Goods*, Depreciation, EquipPurc* | 자산·구매 | GoodsMaster, InstrumentAcq |
| Resolution*, Acct*, Buss | 결재·회계 | ResolutionMaster, ResolutionDetail, AcctCode |
| SGB_ | 기부 | SGB_GibuInfo_M, SGB_GiGibon_M |
| KEDI_, TF_Tonggye* | 통계 | KEDI_*, COR_TonggyeSosogCd_M |
| SSE_Gangsa* | 강사료 | SSE_GangsaSalaryBasic_M |
| Organization, COR_ | 조직·공통 | Organization, COR_SosogCd_M, BaseCode |

## 공통코드 매핑 규칙
모든 코드 컬럼은 공통코드 마스터 **`BaseCode`**(`GroupCode`+`SubCode`)와 연결된다.
```sql
left join BaseCode B on 업무테이블.코드컬럼 = B.SubCode and B.GroupCode = 'A401'
-- 또는
dbo.SF_COR_GongtongCd_Johoe('0', 'A201' + IA.GoodsDiv)
dbo.ADM_Pay_GetSubCodeInfo_Sf('0', 'A401', a.JikGubCode)
```

자주 쓰는 컬럼 ↔ 코드:
| 컬럼 | 그룹 | 공통코드 |
|---|---|---|
| JikGubCode | A401 | [직급](../인사/직급(A401).md) |
| JikJongCode | A402 | [직종코드](../인사/직종코드(A402).md) |
| RoleCode | A403 | [보직코드](../인사/보직코드(A403).md) |
| StaffDiv | A422 | [교직원구분](../인사/교직원구분(A422).md) |
| StaffState | A411 | [재직구분](../인사/재직구분(A411).md) |
| LastAppmntDiv / AppmntDiv | A405 | [임용구분](../인사/임용구분(A405).md) |
| WorkArea | A400 | [근무지역](../인사/근무지역(A400).md) |
| GradDiv | A404 | [학위구분](../인사/학위구분(A404).md) |
| FullTimeYN 등 | A445 | [고용형태](../인사/고용형태(A445).md) |
| Nationality | C011 | [국가코드](../../공통/국가코드(C011).md) |
| AccountBank | C101 | [은행코드](../../공통/은행코드(C101).md) |
| GoodsDiv | A201 | [물품구분](../자산/물품구분(A201).md) |
| AcquisitionDiv | A304 | [취득구분](../자산/취득구분(A304).md) |
| AssetStatus | A303 | [자산상태](../자산/자산상태(A303).md) |
| ResolutionDiv | A113 | [결의서구분](../결재/결의서구분(A113).md) |
| CtrlDiv | A110 | [통제여부](../결재/통제여부(A110).md) |
| ProcessState | A109 | [결재진행상태](../결재/결재진행상태(A109).md) |
| GibuGb | SQ10 | [기부구분](../기부/기부구분(SQ10).md) |
| Gyeyeol5Gb | CZ17 | [계열5구분](../통계/계열5구분(CZ17).md) |

## 급여 계열 공통코드 (knowledge 등록 완료)
[knowledge/행정/급여](../급여/index.md): [공제코드 A501](../급여/공제코드(A501).md) · [지급수당 A502](../급여/지급수당(A502).md) · [지급대상 A506](../급여/지급대상(A506).md) · [호봉코드 A512](../급여/호봉코드(A512).md) · [급여구분 A519](../급여/급여구분(A519).md)

## 비고
- 행정 폴더는 운영 **소스코드(JSP/Java)와 인수인계 쿼리**가 혼재. 본 정리는 `주은 인수자료\쿼리`·`2025`·SP 정의를 기준으로 한 관찰값이며 DDL 전수는 아니다.
- 한글 주석은 원본이 CP949라 일부 도구에서 깨질 수 있음(테이블/컬럼명은 영문).
- 테이블명 대소문자가 자료마다 혼용됨(StaffMaster/staffmaster 등) — DB는 대소문자 비구분.
