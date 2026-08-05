UNIT 8 SPECIFICATION
LeadHunterPro — Sprint 2.3A
Unit 8 — Direct Website Discovery Plugin
Status: APPROVED IMPLEMENTATION SPECIFICATION
1. PURPOSE

Unit 8 is the final implementation unit of Sprint 2.3A.

Its responsibility is ONLY to connect the infrastructure created in Units 1–7 into a working Direct Website Discovery Plugin.

Unit 8 does not redesign or replace any previous unit.

It acts only as the orchestration layer.

2. GOAL

Implement a production-ready Direct Website Discovery Plugin that integrates with the existing Plugin Framework and uses the completed discovery infrastructure.

The plugin shall:

receive discovery requests
generate website candidates
remove duplicate URLs
crawl websites
parse HTML
execute available extractors
aggregate extraction results
calculate confidence
return discovered companies

without modifying any infrastructure built in Units 1–7.

3. UNIT OWNERSHIP

Unit 8 owns ONLY:

Direct Website Discovery Plugin
plugin orchestration
plugin lifecycle
discovery workflow
pipeline coordination

Unit 8 DOES NOT own:

URL normalization
URL filtering
confidence scoring
evidence models
extractor interfaces
PluginConfigView
crawler infrastructure
HTML parser
plugin framework
source orchestrator
4. FROZEN COMPONENTS

The following components are COMPLETE and MUST NOT be modified.

Unit 1

normalize_url

canonical_key

extract_host

Unit 2

DuplicateURLFilter

Unit 3

Confidence

ConfidenceLevel

Unit 4

ExtractionResult

FieldEvidence

EvidenceSet

Unit 5

ALL extractor interfaces

PageContent Protocol

Unit 6

Candidate

CandidateGenerator

Unit 7

PluginConfigView

make_config_view

Existing Framework

BaseDiscoveryPlugin

PluginManager

PluginSource

SourceOrchestrator

HTTPCrawler

HTMLParser

RobotsManager

No behavioural changes are permitted.

5. PRIMARY RESPONSIBILITY

Implement ONE production plugin:

DFbYeRs2NDmaT1pcTMNzQmtrvhURGhLmg3

This plugin must inherit from

BaseDiscoveryPlugin

and integrate into the existing Plugin Framework.

6. DISCOVERY PIPELINE

The plugin SHALL execute the following pipeline.

Discovery Request

↓

Candidate Generation

↓

DuplicateURLFilter

↓

HTTPCrawler

↓

HTMLParser

↓

Field Extractors

↓

Evidence Collection

↓

Confidence Calculation

↓

Company Assembly

↓

Plugin Result

The order SHALL NOT change.

7. CONFIGURATION

Plugin configuration MUST use

PluginConfigView

No direct dictionary access is allowed inside the plugin.

All configuration must be accessed through the view.

8. DEPENDENCIES

Allowed dependencies:

BaseDiscoveryPlugin
PluginConfigView
CandidateGenerator
Candidate
DuplicateURLFilter
HTTPCrawler
HTMLParser
FieldExtractor interfaces
Evidence models
Confidence
Plugin framework

No additional architecture may be introduced.

9. FORBIDDEN

Unit 8 MUST NOT:

modify Units 1–7
modify crawler implementation
modify HTML parser
modify confidence logic
modify evidence models
modify PluginManager
modify PluginSource
modify SourceOrchestrator
10. IMPLEMENTATION RULES

The implementation must:

reuse existing infrastructure
contain no duplicated logic
introduce no parallel implementation of existing functionality
avoid hidden behaviour
preserve backward compatibility
11. PUBLIC API

The plugin must expose only the required public interface defined by BaseDiscoveryPlugin.

No extra public helper classes should be exported.

Internal helpers remain private.

12. ERROR HANDLING

The plugin must:

never silently swallow exceptions
return valid SourceStatus values
preserve crawler failures as metadata
continue processing remaining candidates when possible
13. TESTING REQUIREMENTS

Unit tests must verify:

plugin registration
configuration loading
candidate pipeline
duplicate filtering
crawler integration (mocked)
parser integration (mocked)
extractor execution
evidence aggregation
confidence calculation
final company assembly
failure handling
metadata generation

Regression tests must prove Units 1–7 remain unchanged.

14. ACCEPTANCE CRITERIA

Implementation is complete only when:

Plugin compiles
Plugin registers correctly
All existing tests pass
New tests pass
Ruff passes
Public API unchanged
No frozen unit modified
No architecture violation introduced
15. OUT OF SCOPE

The following are NOT part of Unit 8:

new crawler features
new extractors
new URL algorithms
AI extraction
database persistence
ranking algorithms
optimisation work
Sprint 2.3B functionality
16. AI IMPLEMENTATION RULES

Before writing any code, the implementation AI MUST:

Read this specification completely.
Verify every dependency exists.
Stop if any requirement is ambiguous.
Never invent architecture.
Never expand scope.
Never modify frozen components.
Ask for clarification instead of assuming.
Implement ONLY what this specification requires.
17. FINAL IMPLEMENTATION CONSTRAINT

If any requested implementation conflicts with this specification, this specification takes precedence.

The implementation AI must stop and report the conflict rather than guessing or extending the design.