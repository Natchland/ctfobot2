const fs = require('fs');
const path = require('path');

/**
 * Configuration options for the project structure generator
 */
const DEFAULT_CONFIG = {
  excludeDirs: ['node_modules', '.git', '.vscode', '.idea', '.venv', '__pycache__'],
  excludeFiles: ['package-lock.json', 'yarn.lock', '.env'],
  textExtensions: ['.js', '.html', '.css', '.json', '.md', '.txt', '.sql', '.ts', '.jsx', '.tsx', '.yaml', '.yml', '.py', '.env', '.gitignore', '.toml'],
  outputFile: 'project-structure.txt'
};

/**
 * Recursively collects all file paths in a directory
 * @param {string} dir - Directory to scan
 * @param {string[]} fileList - Accumulator for file paths
 * @param {Object} config - Configuration options
 * @returns {string[]} List of all file paths
 */
function getAllFiles(dir, fileList = [], config = DEFAULT_CONFIG) {
  const files = fs.readdirSync(dir);
  
  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);
    
    if (stat.isDirectory()) {
      // Skip excluded directories
      if (!config.excludeDirs.includes(file)) {
        getAllFiles(filePath, fileList, config);
      }
    } else {
      fileList.push(filePath);
    }
  });
  
  return fileList;
}

/**
 * Determines if a file is text-based (readable) or binary
 * @param {string} filePath - Path to file
 * @param {Object} config - Configuration options
 * @returns {'text' | 'binary'} File type
 */
function getFileType(filePath, config = DEFAULT_CONFIG) {
  const ext = path.extname(filePath).toLowerCase();
  return config.textExtensions.includes(ext) ? 'text' : 'binary';
}

/**
 * Generates a formatted directory tree structure
 * @param {string} dir - Directory to traverse
 * @param {string} prefix - Indentation prefix for tree structure
 * @param {Object} config - Configuration options
 * @returns {string[]} Formatted tree lines
 */
function generateTree(dir, prefix = '', config = DEFAULT_CONFIG) {
  let lines = [];
  const files = fs.readdirSync(dir);
  
  // Separate directories and files
  const dirs = files.filter(f => {
    const stat = fs.statSync(path.join(dir, f));
    return stat.isDirectory() && !config.excludeDirs.includes(f);
  }).sort();
  
  const fileNames = files.filter(f => {
    const stat = fs.statSync(path.join(dir, f));
    return stat.isFile() && 
           !config.excludeFiles.includes(f) && 
           f !== path.basename(__filename);
  }).sort();
  
  // Add files to tree
  fileNames.forEach((file, index) => {
    const isLast = index === fileNames.length - 1 && dirs.length === 0;
    lines.push(`${prefix}${isLast ? '└── ' : '├── '}${file}`);
  });
  
  // Add directories to tree
  dirs.forEach((dirName, index) => {
    const isLast = index === dirs.length - 1;
    lines.push(`${prefix}${isLast ? '└── ' : '├── '}${dirName}/`);
    lines = lines.concat(generateTree(path.join(dir, dirName), prefix + (isLast ? '    ' : '│   '), config));
  });
  
  return lines;
}

/**
 * Main function to generate project documentation
 * @param {Object} options - Configuration options
 * @returns {string} Complete project structure documentation
 */
function generateStructure(options = {}) {
  const config = { ...DEFAULT_CONFIG, ...options };
  const output = [];
  const projectRoot = '.';
  
  // Header
  output.push('====================================');
  output.push('PROJECT STRUCTURE AND CONTENTS');
  output.push('====================================');
  output.push('');
  
  // Directory tree section
  output.push('PROJECT STRUCTURE:');
  output.push('==================');
  output.push(...generateTree(projectRoot, '', config));
  output.push('');
  
  // File contents section
  output.push('FILE CONTENTS:');
  output.push('==============');
  output.push('');
  
  // Get and filter files
  const files = getAllFiles(projectRoot, [], config)
    .filter(file => {
      // Check if file is in any excluded directory
      return !config.excludeDirs.some(dir => file.includes(dir));
    })
    .filter(file => !config.excludeFiles.some(exFile => file.includes(exFile)))
    .filter(file => file !== path.basename(__filename)) // Exclude self
    .sort();
  
  // Process each file
  files.forEach(filePath => {
    output.push(`FILE: ${filePath}`);
    output.push('----------------------------------------');
    
    try {
      if (getFileType(filePath, config) === 'text') {
        const content = fs.readFileSync(filePath, 'utf8');
        output.push(content);
      } else {
        output.push('[BINARY FILE - CONTENT NOT DISPLAYED]');
      }
    } catch (err) {
      output.push(`[ERROR READING FILE: ${err.message}]`);
    }
    
    output.push('');
    output.push('');
  });
  
  return output.join('\n');
}

/**
 * Execute the generator and save to file
 * @param {Object} options - Configuration options
 */
function executeGenerator(options = {}) {
  const config = { ...DEFAULT_CONFIG, ...options };
  const structure = generateStructure(config);
  fs.writeFileSync(config.outputFile, structure);
  
  console.log('Project structure generated successfully!');
  console.log(`Output saved to: ${config.outputFile}`);
}

// Export functions for modular use
module.exports = {
  DEFAULT_CONFIG,
  getAllFiles,
  getFileType,
  generateTree,
  generateStructure,
  executeGenerator
};

// Only execute if this file is run directly (not imported)
if (require.main === module) {
  executeGenerator();
}