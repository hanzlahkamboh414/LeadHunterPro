/**
 * Research-form reference data — the estimation trades the company targets,
 * US states, and each state's major cities.
 *
 * The trade list is the founder's exact list (do not re-order or rename).
 * The city lists are CURATED MAJOR CITIES per state — not every town; the
 * admin form offers a "Custom…" escape hatch for anything not listed.
 */

export const TRADES = [
  "GC",
  "Electrical",
  "Mechanical",
  "Plumbing",
  "Lumber",
  "Demolition",
  "MEP",
  "DryWall",
  "Roofing",
  "Concrete",
  "LandScaping",
  "Flooring",
  "Painting",
  "Finishes",
] as const;

export const US_STATES = [
  "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
  "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
  "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
  "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
  "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
  "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
  "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
  "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
  "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
] as const;

/** Major cities per state — "City, ST" form (what the search location expects). */
export const CITIES_BY_STATE: Record<string, string[]> = {
  Alabama: ["Birmingham, AL", "Montgomery, AL", "Huntsville, AL", "Mobile, AL", "Tuscaloosa, AL"],
  Alaska: ["Anchorage, AK", "Fairbanks, AK", "Juneau, AK"],
  Arizona: ["Phoenix, AZ", "Tucson, AZ", "Mesa, AZ", "Scottsdale, AZ", "Gilbert, AZ", "Chandler, AZ", "Tempe, AZ"],
  Arkansas: ["Little Rock, AR", "Fayetteville, AR", "Fort Smith, AR", "Springdale, AR"],
  California: ["Los Angeles, CA", "San Diego, CA", "San Jose, CA", "San Francisco, CA", "Fresno, CA", "Sacramento, CA", "Long Beach, CA", "Oakland, CA", "Bakersfield, CA", "Anaheim, CA", "Irvine, CA", "Riverside, CA"],
  Colorado: ["Denver, CO", "Colorado Springs, CO", "Aurora, CO", "Fort Collins, CO", "Boulder, CO", "Lakewood, CO"],
  Connecticut: ["Bridgeport, CT", "New Haven, CT", "Hartford, CT", "Stamford, CT", "Waterbury, CT"],
  Delaware: ["Wilmington, DE", "Dover, DE", "Newark, DE"],
  "District of Columbia": ["Washington, DC"],
  Florida: ["Jacksonville, FL", "Miami, FL", "Tampa, FL", "Orlando, FL", "St. Petersburg, FL", "Hialeah, FL", "Tallahassee, FL", "Fort Lauderdale, FL", "Cape Coral, FL", "Naples, FL"],
  Georgia: ["Atlanta, GA", "Augusta, GA", "Columbus, GA", "Savannah, GA", "Athens, GA", "Macon, GA"],
  Hawaii: ["Honolulu, HI", "Pearl City, HI", "Hilo, HI"],
  Idaho: ["Boise, ID", "Meridian, ID", "Nampa, ID", "Idaho Falls, ID"],
  Illinois: ["Chicago, IL", "Aurora, IL", "Naperville, IL", "Joliet, IL", "Rockford, IL", "Springfield, IL", "Peoria, IL"],
  Indiana: ["Indianapolis, IN", "Fort Wayne, IN", "Evansville, IN", "South Bend, IN", "Carmel, IN"],
  Iowa: ["Des Moines, IA", "Cedar Rapids, IA", "Davenport, IA", "Iowa City, IA"],
  Kansas: ["Wichita, KS", "Overland Park, KS", "Kansas City, KS", "Topeka, KS", "Olathe, KS"],
  Kentucky: ["Louisville, KY", "Lexington, KY", "Bowling Green, KY", "Owensboro, KY"],
  Louisiana: ["New Orleans, LA", "Baton Rouge, LA", "Shreveport, LA", "Lafayette, LA", "Lake Charles, LA"],
  Maine: ["Portland, ME", "Lewiston, ME", "Bangor, ME"],
  Maryland: ["Baltimore, MD", "Columbia, MD", "Germantown, MD", "Silver Spring, MD", "Rockville, MD", "Frederick, MD"],
  Massachusetts: ["Boston, MA", "Worcester, MA", "Springfield, MA", "Cambridge, MA", "Lowell, MA", "Brockton, MA"],
  Michigan: ["Detroit, MI", "Grand Rapids, MI", "Ann Arbor, MI", "Lansing, MI", "Flint, MI", "Sterling Heights, MI"],
  Minnesota: ["Minneapolis, MN", "Saint Paul, MN", "Rochester, MN", "Duluth, MN", "Bloomington, MN"],
  Mississippi: ["Jackson, MS", "Gulfport, MS", "Southaven, MS", "Hattiesburg, MS"],
  Missouri: ["Kansas City, MO", "Saint Louis, MO", "Springfield, MO", "Columbia, MO", "Independence, MO"],
  Montana: ["Billings, MT", "Missoula, MT", "Great Falls, MT", "Bozeman, MT"],
  Nebraska: ["Omaha, NE", "Lincoln, NE", "Bellevue, NE", "Grand Island, NE"],
  Nevada: ["Las Vegas, NV", "Henderson, NV", "Reno, NV", "North Las Vegas, NV", "Sparks, NV"],
  "New Hampshire": ["Manchester, NH", "Nashua, NH", "Concord, NH"],
  "New Jersey": ["Newark, NJ", "Jersey City, NJ", "Paterson, NJ", "Elizabeth, NJ", "Trenton, NJ", "Princeton, NJ"],
  "New Mexico": ["Albuquerque, NM", "Las Cruces, NM", "Santa Fe, NM", "Rio Rancho, NM"],
  "New York": ["New York City, NY", "Buffalo, NY", "Rochester, NY", "Yonkers, NY", "Syracuse, NY", "Albany, NY", "Brooklyn, NY", "Queens, NY"],
  "North Carolina": ["Charlotte, NC", "Raleigh, NC", "Greensboro, NC", "Durham, NC", "Winston-Salem, NC", "Fayetteville, NC", "Cary, NC"],
  "North Dakota": ["Fargo, ND", "Bismarck, ND", "Grand Forks, ND", "Minot, ND"],
  Ohio: ["Columbus, OH", "Cleveland, OH", "Cincinnati, OH", "Toledo, OH", "Akron, OH", "Dayton, OH"],
  Oklahoma: ["Oklahoma City, OK", "Tulsa, OK", "Norman, OK", "Broken Arrow, OK"],
  Oregon: ["Portland, OR", "Salem, OR", "Eugene, OR", "Gresham, OR", "Bend, OR"],
  Pennsylvania: ["Philadelphia, PA", "Pittsburgh, PA", "Allentown, PA", "Erie, PA", "Reading, PA", "Scranton, PA"],
  "Rhode Island": ["Providence, RI", "Warwick, RI", "Cranston, RI", "Pawtucket, RI"],
  "South Carolina": ["Columbia, SC", "Charleston, SC", "North Charleston, SC", "Greenville, SC", "Rock Hill, SC"],
  "South Dakota": ["Sioux Falls, SD", "Rapid City, SD", "Aberdeen, SD"],
  Tennessee: ["Nashville, TN", "Memphis, TN", "Knoxville, TN", "Chattanooga, TN", "Clarksville, TN"],
  Texas: ["Houston, TX", "Dallas, TX", "Austin, TX", "San Antonio, TX", "Fort Worth, TX", "El Paso, TX", "Arlington, TX", "Plano, TX", "Corpus Christi, TX", "Frisco, TX", "Laredo, TX", "Lubbock, TX", "Irving, TX", "Garland, TX", "McKinney, TX", "Amarillo, TX"],
  Utah: ["Salt Lake City, UT", "West Valley City, UT", "Provo, UT", "Orem, UT", "St. George, UT"],
  Vermont: ["Burlington, VT", "Montpelier, VT"],
  Virginia: ["Virginia Beach, VA", "Richmond, VA", "Norfolk, VA", "Arlington, VA", "Chesapeake, VA", "Alexandria, VA", "Reston, VA"],
  Washington: ["Seattle, WA", "Spokane, WA", "Tacoma, WA", "Vancouver, WA", "Bellevue, WA", "Everett, WA", "Kent, WA"],
  "West Virginia": ["Charleston, WV", "Huntington, WV", "Morgantown, WV", "Parkersburg, WV"],
  Wisconsin: ["Milwaukee, WI", "Madison, WI", "Green Bay, WI", "Kenosha, WI", "Racine, WI", "Appleton, WI"],
  Wyoming: ["Cheyenne, WY", "Casper, WY", "Laramie, WY", "Gillette, WY"],
};

/** Full state name -> 2-letter postal code. The Phones vertical's API takes
 *  the 2-letter form (license boards key on it); the email Research form keeps
 *  using the "City, ST" strings above. */
export const US_STATE_CODES: Record<string, string> = {
  Alabama: "AL", Alaska: "AK", Arizona: "AZ", Arkansas: "AR", California: "CA",
  Colorado: "CO", Connecticut: "CT", Delaware: "DE", Florida: "FL",
  Georgia: "GA", Hawaii: "HI", Idaho: "ID", Illinois: "IL", Indiana: "IN",
  Iowa: "IA", Kansas: "KS", Kentucky: "KY", Louisiana: "LA", Maine: "ME",
  Maryland: "MD", Massachusetts: "MA", Michigan: "MI", Minnesota: "MN",
  Mississippi: "MS", Missouri: "MO", Montana: "MT", Nebraska: "NE",
  Nevada: "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
  "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", Ohio: "OH",
  Oklahoma: "OK", Oregon: "OR", Pennsylvania: "PA", "Rhode Island": "RI",
  "South Carolina": "SC", "South Dakota": "SD", Tennessee: "TN", Texas: "TX",
  Utah: "UT", Vermont: "VT", Virginia: "VA", Washington: "WA",
  "West Virginia": "WV", Wisconsin: "WI", Wyoming: "WY",
  "District of Columbia": "DC",
};
