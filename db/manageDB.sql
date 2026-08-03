-- -- schema.sql
-- CREATE DATABASE IF NOT EXISTS factsheets;
-- USE factsheets;

-- CREATE TABLE IF NOT EXISTS amc_sites (
--     id                  INT AUTO_INCREMENT PRIMARY KEY,
--     amc_name            VARCHAR(100) NOT NULL,
--     downloads_page_url  VARCHAR(500) NOT NULL,
--     created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
--     UNIQUE KEY unique_amc (amc_name)
-- );

-- INSERT INTO amc_sites (amc_name, downloads_page_url)
-- VALUES ('360 ONE Mutual Fund', 'https://www.360.one/asset/mutual-funds/downloads/');
-- insert_remaining_amcs.sql
USE factsheets;

-- INSERT INTO amc_sites (amc_name, downloads_page_url) VALUES
-- ('Abakkus Mutual Fund', 'https://www.abakkusmf.com/factsheet.html#'),
-- ('Aditya Birla Sun Life Mutual Fund', 'https://mutualfund.adityabirlacapital.com/forms-and-downloads/factsheets'),
-- ('Angel One Mutual Fund', 'https://www.angelonemf.com/downloads'),
-- ('Axis Mutual Fund', 'https://www.axismf.com/downloads'),
-- ('Bajaj Finserv Mutual Fund', 'https://www.bajajamc.com/downloads?factsheet'),
-- ('Bandhan Mutual Fund', 'https://bandhanmutual.com/downloads/factsheet/all-schemes'),
-- ('Bank of India Mutual Fund', 'https://www.boimf.in/investor-corner'),
-- ('Baroda BNP Paribas Mutual Fund', 'https://www.barodabnpparibasmf.in/downloads/monthly-factsheet'),
-- ('Canara Robeco Mutual Fund', 'https://www.canararobeco.com/documents/forms-downloads/forms-information-documents/information-documents/factsheets/'),
-- ('Capitalmind Mutual Fund', 'https://capitalmindmf.com/factsheet.html#'),
-- ('Choice Mutual Fund', 'https://choicemf.com/disclosures/factsheets'),
-- ('DSP Mutual Fund', 'https://www.dspim.com/downloads?category=Information%20Documents&sub_category=Factsheets'),
-- ('Edelweiss Mutual Fund', 'https://www.edelweissmf.com/downloads/factsheets'),
-- ('Franklin Templeton Mutual Fund', 'https://www.franklintempletonindia.com/downloads/fund-literature'),
-- ('Groww Mutual Fund', 'https://www.growwmf.in/downloads/fact-sheet'),
-- ('HDFC Mutual Fund', 'https://www.hdfcfund.com/mutual-funds/factsheets'),
-- ('Helios Mutual Fund', 'https://www.heliosmf.in/downloads'),
-- ('HSBC Mutual Fund', 'https://www.assetmanagement.hsbc.co.in/en/mutual-funds/investor-resources?Date=&Cap=&Doc=fund-factsheets'),
-- ('ICICI Prudential Mutual Fund', 'https://www.icicipruamc.com/media-center/downloads?currentTabFilter=Historical%20Factsheets'),
-- ('Invesco Mutual Fund', 'https://www.invescomutualfund.com/literature-forms/factsheets'),
-- ('ITI Mutual Fund', 'https://www.itiamc.com/downloads'),
-- ('Jio BlackRock Mutual Fund', 'https://www.jioblackrockamc.com/statutory-disclosure/fund-documents/factsheet'),
-- ('JM Financial Mutual Fund', 'https://www.jmfinancialmf.com/downloads/Factsheet/Factsheet'),
-- ('Kotak Mahindra Mutual Fund', 'https://www.kotakmf.com/Information/forms-and-downloads/Factsheet'),
-- ('LIC Mutual Fund', 'https://www.licmf.com/downloads/factsheet'),
-- ('Mahindra Manulife Mutual Fund', 'https://www.mahindramanulife.com/downloads#investor'),
-- ('Mirae Asset Mutual Fund', 'https://www.miraeassetmf.co.in/downloads/factsheet'),
-- ('Motilal Oswal Mutual Fund', 'https://www.motilaloswalmf.com/downloads/factsheets'),
-- ('Navi Mutual Fund', 'https://navi.com/mutual-fund/downloads/factsheet'),
-- ('Nippon India Mutual Fund', 'https://mf.nipponindiaim.com/investor-service/downloads/factsheet-portfolio-and-other-disclosures'),
-- ('NJ Mutual Fund', 'https://downloads.njmutualfund.com/downloads.php'),
-- ('Old Bridge Mutual Fund', 'https://oldbridgemf.com/factsheet.html'),
-- ('PGIM India Mutual Fund', 'https://www.pgimindia.com/mutual-funds/forms-and-product-updates/Fund-Factsheet'),
-- ('PPFAS Mutual Fund', 'https://amc.ppfas.com/downloads/factsheet/'),
-- ('quant Mutual Fund', 'https://quantmutual.com/downloads/factsheet'),
-- ('Quantum Mutual Fund', 'https://www.quantumamc.com/factsheets/combined/-1/0/0'),
-- ('Samco Mutual Fund', 'https://www.samcomf.com/downloads'),
-- ('SBI Mutual Fund', 'https://www.sbimf.com/factsheets'),
-- ('Shriram Mutual Fund', 'https://www.shriramamc.in/factsheet'),
-- ('Sundaram Mutual Fund', 'https://www.sundarammutual.com/fundwise-factsheet'),
-- ('Tata Mutual Fund', 'https://www.tatamutualfund.com/information-documents/factsheets'),
-- ('Taurus Mutual Fund', 'https://www.taurusmutualfund.com/factsheet'),
-- ('Trust Mutual Fund', 'https://www.trustmf.com/downloads?activeTab=factsheets'),
-- ('Unifi Mutual Fund', 'https://unifimf.com/factsheet/'),
-- ('Union Mutual Fund', 'https://www.unionmf.com/about-us/downloads'),
-- ('UTI Mutual Fund', 'https://www.utimf.com/downloads/fact-sheet'),
-- ('The Wealth Company Mutual Fund', 'https://www.wealthcompanyamc.in/literature-forms/scheme-documents/factsheets/'),
-- ('WhiteOak Capital Mutual Fund', 'https://mf.whiteoakamc.com/resources?resource-type=downloads&category=factsheet&page=1&subCategory=monthly-factsheet'),
-- ('Zerodha Mutual Fund', 'https://www.zerodhafundhouse.com/resources/fund-documents');

-- ALTER TABLE amc_sites
-- ADD COLUMN interaction_steps JSON NULL
-- AFTER downloads_page_url;
-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', 'button:has-text("Agree")'
--     ),
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', 'button[data-tab-value="factSheets"]',
--         'wait_for', '#tab-panel-factsheets'
--     )
-- )
-- WHERE amc_name = '360 ONE Mutual Fund';

-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', 'button[role=''combobox'']'
--     ),
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', 'button[role=''option'']',
--         'nth', 1
--     )
-- )
-- WHERE amc_name = 'Groww Mutual Fund';
-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', 'button[aria-controls="investor-fund-factsheet-panel"]'
--     ),
--     JSON_OBJECT(
--         'action', 'wait',
--         'ms', 1500
--     )
-- )
-- WHERE amc_name = 'Mahindra Manulife Mutual Fund';

-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', '//div[contains(@class,"cs-accordion-header")][.//span[normalize-space()="Factsheets"]]'
--     ),
--     JSON_OBJECT(
--         'action', 'wait',
--         'ms', 1500
--     )
-- )
-- WHERE amc_name = 'Navi Mutual Fund';

-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', '//a[normalize-space()="Consolidated Factsheet"]'
--     ),
--     JSON_OBJECT(
--         'action', 'wait',
--         'ms', 1500
--     )
-- )
-- WHERE amc_name = 'Sundaram Mutual Fund';

-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', '//a[@id="accord-link-2"]',
--         'wait_for', '//a[@data-tag="factsheets"]'
--     ),
--     JSON_OBJECT(
--         'action', 'click',
--         'selector', '//a[@data-tag="factsheets"]',
--         'disable_chat', TRUE,
--         'verify_click', TRUE,
--         'wait_for_pdf', TRUE,
--         'settle_ms', 1000
--     )
-- )
-- WHERE amc_name = 'Union Mutual Fund';


-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--         JSON_OBJECT(
--         'action','select_latest_option',
--         'dropdown_selector','div[aria-labelledby="month-select-label"]',
--         'option_selector','ul[role="listbox"] li[role="option"]',
--         'value_type','month'
--     )
-- )
-- WHERE amc_name = 'Choice Mutual Fund';

-- UPDATE amc_sites
-- SET interaction_steps = JSON_ARRAY(
--     JSON_OBJECT(
--         'action','wait',
--         'ms',3000
--     )
-- )
-- WHERE amc_name='Edelweiss Mutual Fund';

UPDATE amc_sites
SET interaction_steps = JSON_ARRAY(
    JSON_OBJECT(
        'action', 'click',
        'selector', 'text="Complete Factsheet"',
        'settle_ms', 800
    ),
    JSON_OBJECT(
        'action', 'click',
        'selector', 'button[name="apply"]',
        'settle_ms', 1500
    )
)
WHERE amc_name = 'ICICI Prudential Mutual Fund';